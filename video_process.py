"""Self-contained lightweight video preprocessing for VLM stage experiments.

This module has no dependency on the original VLM Auto Annotation codebase.
It converts a single video or multi-view videos into OpenAI-compatible media
message parts and returns sampling metadata.
"""

from __future__ import annotations

import base64
import json
import math
from pathlib import Path
import tempfile
from typing import Any

import cv2
import numpy as np


class VideoProcessError(RuntimeError):
    """Raised when video preprocessing fails."""


def _normalize_jpeg_quality(value: int) -> int:
    try:
        quality = int(value)
    except Exception as exc:  # pragma: no cover - defensive
        raise VideoProcessError(f"jpeg_quality must be an integer, got {value!r}") from exc
    if not 1 <= quality <= 100:
        raise VideoProcessError(f"jpeg_quality must be in [1, 100], got {quality}")
    return quality


def _validate_video_config(
    *,
    fps: float,
    max_frames: int,
    max_time: float | int,
    resize_width: int,
    input_mode: str,
    merge_mode: str,
    min_api_frames: int,
) -> None:
    if fps <= 0:
        raise VideoProcessError(f"fps must be > 0, got {fps}")
    if max_frames <= 0:
        raise VideoProcessError(f"max_frames must be > 0, got {max_frames}")
    if resize_width <= 0:
        raise VideoProcessError(f"resize_width must be > 0, got {resize_width}")
    if min_api_frames <= 0:
        raise VideoProcessError(f"min_api_frames must be > 0, got {min_api_frames}")
    if max_time != -1 and max_time <= 0:
        raise VideoProcessError(f"max_time must be -1 or > 0 seconds, got {max_time}")
    if input_mode not in {"image_sequence", "video"}:
        raise VideoProcessError(f"input_mode must be 'image_sequence' or 'video', got {input_mode!r}")
    if merge_mode not in {"per_frame", "timeline_grid"}:
        raise VideoProcessError(f"merge_mode must be 'per_frame' or 'timeline_grid', got {merge_mode!r}")


def _normalize_max_time(value: float | int | None) -> float:
    if value is None:
        return -1.0
    try:
        max_time = float(value)
    except Exception as exc:  # pragma: no cover - defensive
        raise VideoProcessError(f"max_time must be -1 or > 0 seconds, got {value!r}") from exc
    if max_time != -1.0 and max_time <= 0:
        raise VideoProcessError(f"max_time must be -1 or > 0 seconds, got {value!r}")
    return max_time


def normalize_video_input(
    video_path: str | Path | list[str | Path] | dict[str, str | Path],
    view_names: list[str] | None = None,
) -> tuple[dict[str, Path], str]:
    """Normalize single/list/dict video inputs into an ordered view map.

    Args:
        video_path: Single path, list of paths, or dict mapping view name to path.
        view_names: Optional view names/order. For dict input, this selects and orders keys.
            For list input, length must match the path list.

    Returns:
        ``(view_map, input_type)`` where ``view_map`` preserves insertion order.
    """
    if isinstance(video_path, dict):
        input_type = "multi_view_dict"
        if not video_path:
            raise VideoProcessError("video_path dict must not be empty")
        if view_names:
            missing = [name for name in view_names if name not in video_path]
            if missing:
                raise VideoProcessError(f"view_names not found in video_path dict: {missing}")
            return {name: Path(video_path[name]) for name in view_names}, input_type
        return {str(name): Path(path) for name, path in video_path.items()}, input_type

    if isinstance(video_path, (list, tuple)):
        input_type = "multi_view_list"
        paths = [Path(path) for path in video_path]
        if not paths:
            raise VideoProcessError("video_path list must not be empty")
        if view_names:
            if len(view_names) != len(paths):
                raise VideoProcessError(
                    f"view_names length ({len(view_names)}) must match video_path list length ({len(paths)})"
                )
            names = list(view_names)
        else:
            names = [f"view_{idx}" for idx in range(len(paths))]
        return dict(zip(names, paths, strict=True)), input_type

    return {view_names[0] if view_names else "primary": Path(video_path)}, "single"


def read_video_info(path: str | Path) -> dict[str, Any]:
    """Read FPS/frame-count/shape metadata from a video file."""
    path = Path(path)
    if not path.exists():
        raise VideoProcessError(f"video file does not exist: {path}")
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise VideoProcessError(f"failed to open video: {path}")
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    finally:
        cap.release()
    if fps <= 0:
        raise VideoProcessError(f"video fps is invalid for {path}: {fps}")
    if frame_count <= 0:
        raise VideoProcessError(f"video has no frames: {path}")
    return {
        "path": str(path),
        "fps": fps,
        "frame_count": frame_count,
        "width": width,
        "height": height,
        "duration": frame_count / fps,
    }


def compute_sample_timestamps(
    *,
    original_fps: float,
    frame_count: int,
    target_fps: float,
    max_frames: int,
    min_api_frames: int = 1,
    frame_start: int = 0,
    frame_end: int | None = None,
) -> tuple[list[int], list[float]]:
    """Compute primary-view sample frame indices and timestamps.

    ``frame_start`` and ``frame_end`` are inclusive source-frame bounds. Timestamps
    always refer to the original video time axis.
    """
    if frame_count <= 0:
        return [], []
    start = max(0, int(frame_start))
    end = frame_count - 1 if frame_end is None else min(frame_count - 1, int(frame_end))
    if start > end:
        raise VideoProcessError(f"invalid frame range: frame_start={frame_start}, frame_end={frame_end}")

    start_t = start / original_fps
    end_t = end / original_fps
    duration_t = max(0.0, end_t - start_t)

    if duration_t == 0:
        indices = [start]
    else:
        step = 1.0 / target_fps
        timestamps = list(np.arange(start_t, end_t + 1e-9, step, dtype=float))
        if not timestamps or timestamps[-1] < end_t - (0.5 / original_fps):
            timestamps.append(end_t)
        indices = sorted({min(end, max(start, int(round(t * original_fps)))) for t in timestamps})

    available = end - start + 1
    desired_min = min(max(int(min_api_frames), 1), available, max_frames)
    if len(indices) < desired_min:
        indices = sorted({int(round(x)) for x in np.linspace(start, end, desired_min)})

    if len(indices) > max_frames:
        indices = sorted({indices[int(round(x))] for x in np.linspace(0, len(indices) - 1, max_frames)})
        # Rounding can produce duplicates; fill deterministically if needed.
        if len(indices) < max_frames:
            for idx in np.linspace(start, end, max_frames * 2):
                indices.append(int(round(idx)))
                indices = sorted(set(indices))
                if len(indices) >= max_frames:
                    break
            indices = indices[:max_frames]

    timestamps = [idx / original_fps for idx in indices]
    return indices, timestamps


def _effective_time_window(
    *,
    infos: dict[str, dict[str, Any]],
    primary_view: str,
    frame_start: int,
    frame_end: int | None,
    max_time: float,
) -> dict[str, Any]:
    primary_info = infos[primary_view]
    primary_fps = float(primary_info["fps"])
    primary_frame_count = int(primary_info["frame_count"])
    original_duration = float(primary_info["duration"])

    start_frame = max(0, int(frame_start))
    if start_frame >= primary_frame_count:
        raise VideoProcessError(f"frame_start out of range: {frame_start}; primary frame_count={primary_frame_count}")
    start_time = start_frame / primary_fps

    duration_limits = {name: float(info["duration"]) for name, info in infos.items()}
    per_view_effective_durations = {
        name: min(duration, max_time) if max_time > 0 else duration for name, duration in duration_limits.items()
    }

    end_limits: list[float] = list(per_view_effective_durations.values())
    frame_range_end_time: float | None = None
    if frame_end is not None:
        bounded_frame_end = min(primary_frame_count - 1, int(frame_end))
        if bounded_frame_end < start_frame:
            raise VideoProcessError(f"invalid frame range: frame_start={frame_start}, frame_end={frame_end}")
        frame_range_end_time = (bounded_frame_end + 1) / primary_fps
        end_limits.append(frame_range_end_time)

    effective_end_time = min(end_limits)
    if effective_end_time <= start_time:
        raise VideoProcessError(
            "effective video interval is empty after applying max_time/frame range/view duration limits: "
            f"start_time={start_time}, effective_end_time={effective_end_time}"
        )

    effective_end_frame = min(primary_frame_count - 1, int(math.ceil(effective_end_time * primary_fps) - 1))
    if effective_end_frame < start_frame:
        raise VideoProcessError(
            "effective frame interval is empty after applying max_time/frame range/view duration limits: "
            f"start_frame={start_frame}, effective_end_frame={effective_end_frame}"
        )

    min_input_duration = min(duration_limits.values())
    configured_limit = max_time if max_time > 0 else None
    configured_limit_applied = configured_limit is not None and any(duration > configured_limit for duration in duration_limits.values())
    was_limited_by_view_duration = min_input_duration < original_duration
    was_limited_by_frame_range = frame_range_end_time is not None and frame_range_end_time <= min(
        per_view_effective_durations.values()
    )

    return {
        "original_duration": original_duration,
        "configured_max_time": max_time if max_time > 0 else -1,
        "effective_duration": max(0.0, effective_end_time - start_time),
        "effective_start_time": start_time,
        "effective_end_time": effective_end_time,
        "effective_frame_start": start_frame,
        "effective_frame_end": effective_end_frame,
        "min_input_duration": min_input_duration,
        "per_view_durations": duration_limits,
        "per_view_effective_durations": per_view_effective_durations,
        "was_time_limited": bool(configured_limit_applied),
        "was_limited_by_view_duration": bool(was_limited_by_view_duration),
        "was_limited_by_frame_range": bool(was_limited_by_frame_range),
    }


def _read_frame(path: Path, frame_index: int) -> np.ndarray:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise VideoProcessError(f"failed to open video while reading frame: {path}")
    try:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_index))
        ok, frame = cap.read()
    finally:
        cap.release()
    if not ok or frame is None:
        raise VideoProcessError(f"failed to read frame {frame_index} from {path}")
    return frame


def resize_keep_aspect(frame: np.ndarray, resize_width: int) -> np.ndarray:
    """Resize a frame to the target width while preserving aspect ratio."""
    h, w = frame.shape[:2]
    if w == resize_width:
        return frame
    scale = resize_width / float(w)
    new_h = max(1, int(round(h * scale)))
    return cv2.resize(frame, (int(resize_width), new_h), interpolation=cv2.INTER_AREA)


def draw_overlay(
    frame: np.ndarray,
    *,
    timestamp: float | None = None,
    view_name: str | None = None,
    draw_timestamps: bool = True,
    draw_view_names: bool = True,
) -> np.ndarray:
    """Draw timestamp/view-name labels on a copy of ``frame``."""
    out = frame.copy()
    labels: list[str] = []
    if draw_view_names and view_name:
        labels.append(str(view_name))
    if draw_timestamps and timestamp is not None:
        labels.append(f"t={timestamp:.2f}s")
    if not labels:
        return out

    text = " | ".join(labels)
    h, w = out.shape[:2]
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = max(0.4, min(1.0, w / 700.0))
    thickness = max(1, int(round(scale * 2)))
    (tw, th), baseline = cv2.getTextSize(text, font, scale, thickness)
    x, y = 8, 8 + th
    cv2.rectangle(out, (4, 4), (min(w - 1, x + tw + 8), min(h - 1, y + baseline + 8)), (0, 0, 0), -1)
    cv2.putText(out, text, (x, y), font, scale, (255, 255, 255), thickness, cv2.LINE_AA)
    return out


def _pad_to_size(frame: np.ndarray, target_h: int, target_w: int) -> np.ndarray:
    h, w = frame.shape[:2]
    pad_bottom = max(0, target_h - h)
    pad_right = max(0, target_w - w)
    return cv2.copyMakeBorder(frame, 0, pad_bottom, 0, pad_right, cv2.BORDER_CONSTANT, value=(245, 245, 245))


def merge_view_frames(frames: list[np.ndarray], *, separator: int = 4) -> np.ndarray:
    """Vertically stitch frames from multiple views with visible separators."""
    if not frames:
        raise VideoProcessError("merge_view_frames received no frames")
    if len(frames) == 1:
        return frames[0]
    max_w = max(frame.shape[1] for frame in frames)
    padded = [_pad_to_size(frame, frame.shape[0], max_w) for frame in frames]
    sep = np.zeros((separator, max_w, 3), dtype=np.uint8)
    pieces: list[np.ndarray] = []
    for idx, frame in enumerate(padded):
        if idx:
            pieces.append(sep)
        pieces.append(frame)
    return cv2.vconcat(pieces)


def merge_temporal_frames(frames: list[np.ndarray], *, columns: int | None = None) -> np.ndarray:
    """Merge consecutive temporal frames into a left-to-right timeline montage."""
    if not frames:
        raise VideoProcessError("merge_temporal_frames received no frames")
    if len(frames) == 1:
        return frames[0]
    n = len(frames)
    cols = columns or n
    rows = int(math.ceil(n / cols))
    max_h = max(frame.shape[0] for frame in frames)
    max_w = max(frame.shape[1] for frame in frames)
    blank = np.full((max_h, max_w, 3), 245, dtype=np.uint8)
    cells = [_pad_to_size(frame, max_h, max_w) for frame in frames]
    while len(cells) < rows * cols:
        cells.append(blank.copy())
    row_imgs = [cv2.hconcat(cells[r * cols : (r + 1) * cols]) for r in range(rows)]
    return cv2.vconcat(row_imgs)


def _format_axis_timestamp(value: Any) -> str:
    try:
        return f"t={float(value):.2f}s"
    except Exception:
        return f"t={value}"


def _ordered_unique(values: list[Any]) -> list[Any]:
    seen: set[Any] = set()
    ordered: list[Any] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def add_montage_axes(
    frame: np.ndarray,
    *,
    column_labels: list[str],
    row_labels: list[str],
    cell_width: int,
    cell_height: int,
) -> np.ndarray:
    """Add timeline column labels and view row labels around a montage."""
    if not column_labels or not row_labels:
        return frame
    top_margin = 34
    left_margin = max(88, min(180, 18 + max(len(label) for label in row_labels) * 9))
    out_h = frame.shape[0] + top_margin
    out_w = frame.shape[1] + left_margin
    out = np.zeros((out_h, out_w, 3), dtype=np.uint8)
    out[top_margin:, left_margin:] = frame

    font = cv2.FONT_HERSHEY_SIMPLEX
    scale = 0.45
    thickness = 1
    text_color = (255, 255, 255)
    line_color = (90, 90, 90)

    for col, label in enumerate(column_labels):
        x0 = left_margin + col * cell_width
        x1 = min(left_margin + (col + 1) * cell_width, out_w)
        center_x = (x0 + x1) // 2
        (tw, th), _ = cv2.getTextSize(label, font, scale, thickness)
        cv2.putText(out, label, (max(left_margin, center_x - tw // 2), 22), font, scale, text_color, thickness, cv2.LINE_AA)
        cv2.line(out, (x0, top_margin - 4), (x0, out_h - 1), line_color, 1)
    cv2.line(out, (out_w - 1, top_margin - 4), (out_w - 1, out_h - 1), line_color, 1)

    row_height = max(1, cell_height // max(1, len(row_labels)))
    for row, label in enumerate(row_labels):
        y0 = top_margin + row * row_height
        y1 = top_margin + (row + 1) * row_height if row + 1 < len(row_labels) else top_margin + cell_height
        center_y = (y0 + y1) // 2
        (tw, th), _ = cv2.getTextSize(label, font, scale, thickness)
        cv2.putText(out, label, (max(4, left_margin - tw - 8), center_y + th // 2), font, scale, text_color, thickness, cv2.LINE_AA)
        cv2.line(out, (left_margin - 4, y0), (out_w - 1, y0), line_color, 1)
    cv2.line(out, (left_margin - 4, top_margin + cell_height), (out_w - 1, top_margin + cell_height), line_color, 1)
    cv2.line(out, (left_margin - 4, top_margin - 4), (left_margin - 4, out_h - 1), line_color, 1)
    return out


def encode_frame_to_image_part(frame: np.ndarray, jpeg_quality: int) -> dict[str, Any]:
    """Encode a BGR frame as an OpenAI-compatible base64 JPEG image part."""
    jpeg_quality = _normalize_jpeg_quality(jpeg_quality)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
    if not ok:
        raise VideoProcessError("cv2.imencode failed while encoding JPEG")
    b64 = base64.b64encode(buf.tobytes()).decode("utf-8")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


def encode_frames_to_video_part(
    frames: list[np.ndarray],
    *,
    timestamps: list[float],
    fallback_fps: float,
) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
    """Encode processed frames as an MP4 ``video_url`` message part."""
    if not frames:
        raise VideoProcessError("cannot encode an empty frame sequence as video")
    output_fps = float(fallback_fps)
    if len(timestamps) > 1 and timestamps[-1] > timestamps[0]:
        output_fps = (len(timestamps) - 1) / (timestamps[-1] - timestamps[0])
    output_fps = max(0.01, output_fps)
    height = max(frame.shape[0] for frame in frames)
    width = max(frame.shape[1] for frame in frames)
    height += height % 2
    width += width % 2

    with tempfile.TemporaryDirectory(prefix="vlm_processed_video_") as temp_dir:
        video_path = Path(temp_dir) / "processed.mp4"
        writer = cv2.VideoWriter(
            str(video_path),
            cv2.VideoWriter_fourcc(*"mp4v"),
            output_fps,
            (width, height),
        )
        if not writer.isOpened():
            raise VideoProcessError("failed to initialize MP4 writer with codec 'mp4v'")
        try:
            for frame in frames:
                writer.write(_pad_to_size(frame, height, width))
        finally:
            writer.release()
        payload = video_path.read_bytes()

    if not payload:
        raise VideoProcessError("processed MP4 encoding produced an empty payload")
    b64 = base64.b64encode(payload).decode("utf-8")
    part = {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{b64}"}}
    return part, payload, {
        "container": "mp4",
        "codec": "mp4v",
        "fps": output_fps,
        "frame_count": len(frames),
        "width": width,
        "height": height,
    }


def _safe_view_dir_name(value: Any) -> str:
    invalid = '<>:"/\\|?*'
    text = str(value or "view")
    cleaned = "".join("_" if ch in invalid or ord(ch) < 32 else ch for ch in text)
    return cleaned.strip(" .") or "view"


def _save_frame_sequence(frames: list[np.ndarray], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old_frame in out_dir.glob("frame_*.jpg"):
        old_frame.unlink()
    for idx, frame in enumerate(frames):
        ok = cv2.imwrite(str(out_dir / f"frame_{idx:06d}.jpg"), frame)
        if not ok:
            raise VideoProcessError(f"failed to save processed frame {idx} to {out_dir}")


def save_processed_frames(
    frames: list[np.ndarray],
    video_meta: dict[str, Any],
    save_processed_path: str | Path,
    *,
    frames_by_view: dict[str, list[np.ndarray]] | None = None,
    video_payloads: dict[str, bytes] | None = None,
) -> None:
    """Save processed frames/videos and ``video_meta.json`` for debugging."""
    out_dir = Path(save_processed_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    cleanup_dirs = [out_dir, *(path for path in out_dir.iterdir() if path.is_dir())]
    for cleanup_dir in cleanup_dirs:
        for old_frame in cleanup_dir.glob("frame_*.jpg"):
            old_frame.unlink()
        old_video = cleanup_dir / "processed.mp4"
        if old_video.exists():
            old_video.unlink()
    if frames_by_view:
        for view_name, view_frames in frames_by_view.items():
            view_dir = out_dir / _safe_view_dir_name(view_name)
            _save_frame_sequence(view_frames, view_dir)
            payload = (video_payloads or {}).get(view_name)
            if payload is not None:
                (view_dir / "processed.mp4").write_bytes(payload)
    else:
        _save_frame_sequence(frames, out_dir)
        payload = (video_payloads or {}).get("__combined__")
        if payload is not None:
            (out_dir / "processed.mp4").write_bytes(payload)
    (out_dir / "video_meta.json").write_text(
        json.dumps(video_meta, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _build_timepoint_frames(
    view_map: dict[str, Path],
    infos: dict[str, dict[str, Any]],
    primary_name: str,
    primary_indices: list[int],
    timestamps: list[float],
    *,
    resize_width: int,
    draw_timestamps: bool,
    draw_view_names: bool,
    merge_views: bool,
) -> tuple[list[np.ndarray], list[dict[str, Any]]]:
    output_frames: list[np.ndarray] = []
    output_groups: list[dict[str, Any]] = []
    selected_views = view_map if merge_views else {primary_name: view_map[primary_name]}

    for out_idx, (primary_idx, timestamp) in enumerate(zip(primary_indices, timestamps, strict=True)):
        view_frames: list[np.ndarray] = []
        per_view_indices: dict[str, int] = {}
        for view_name, path in selected_views.items():
            info = infos[view_name]
            view_index = int(round(timestamp * float(info["fps"])))
            view_index = min(max(0, view_index), int(info["frame_count"]) - 1)
            if view_name == primary_name:
                view_index = min(primary_idx, int(info["frame_count"]) - 1)
            per_view_indices[view_name] = view_index
            frame = _read_frame(path, view_index)
            frame = resize_keep_aspect(frame, resize_width)
            frame = draw_overlay(
                frame,
                timestamp=timestamp,
                view_name=view_name,
                draw_timestamps=draw_timestamps,
                draw_view_names=draw_view_names,
            )
            view_frames.append(frame)

        if merge_views:
            merged = merge_view_frames(view_frames)
            output_frames.append(merged)
            output_groups.append(
                {
                    "output_index": len(output_frames) - 1,
                    "primary_frame_indices": [primary_idx],
                    "timestamps": [timestamp],
                    "per_view_frame_indices": per_view_indices,
                    "views": list(selected_views.keys()),
                }
            )
        else:
            for view_name, frame in zip(selected_views.keys(), view_frames, strict=True):
                output_frames.append(frame)
                output_groups.append(
                    {
                        "output_index": len(output_frames) - 1,
                        "primary_frame_indices": [primary_idx],
                        "timestamps": [timestamp],
                        "per_view_frame_indices": {view_name: per_view_indices[view_name]},
                        "views": [view_name],
                    }
                )

    return output_frames, output_groups


def _apply_temporal_merge(
    frames: list[np.ndarray],
    groups: list[dict[str, Any]],
    *,
    merge_mode: str,
    merge_length: int,
    draw_montage_axes: bool,
) -> tuple[list[np.ndarray], list[dict[str, Any]]]:
    if merge_mode != "timeline_grid":
        return frames, groups
    effective_merge_length = len(frames) if merge_length < 1 else merge_length
    if effective_merge_length <= 1:
        return frames, groups
    merged_frames: list[np.ndarray] = []
    merged_groups: list[dict[str, Any]] = []
    for start in range(0, len(frames), effective_merge_length):
        chunk_frames = frames[start : start + effective_merge_length]
        chunk_groups = groups[start : start + effective_merge_length]
        merged = merge_temporal_frames(chunk_frames, columns=len(chunk_frames))
        chunk_views = list(chunk_groups[0].get("views", [])) if chunk_groups else []
        if draw_montage_axes and len(chunk_frames) > 1 and len(chunk_views) > 1:
            merged = add_montage_axes(
                merged,
                column_labels=[_format_axis_timestamp(g.get("timestamps", [""])[0]) for g in chunk_groups],
                row_labels=[str(view) for view in chunk_views],
                cell_width=max(frame.shape[1] for frame in chunk_frames),
                cell_height=max(frame.shape[0] for frame in chunk_frames),
            )
        merged_frames.append(merged)
        merged_groups.append(
            {
                "output_index": len(merged_frames) - 1,
                "source_output_indices": [g["output_index"] for g in chunk_groups],
                "frame_indices": [idx for g in chunk_groups for idx in g.get("primary_frame_indices", [])],
                "timestamps": [ts for g in chunk_groups for ts in g.get("timestamps", [])],
                "views": _ordered_unique([view for g in chunk_groups for view in g.get("views", [])]),
            }
        )
    return merged_frames, merged_groups


def _process_view_set(
    view_map: dict[str, Path],
    infos: dict[str, dict[str, Any]],
    primary_view: str,
    *,
    fps: float,
    max_frames: int,
    max_time: float,
    resize_width: int,
    draw_timestamps: bool,
    draw_view_names: bool,
    min_api_frames: int,
    frame_start: int,
    frame_end: int | None,
    merge_views: bool,
    merge_mode: str,
    merge_length: int,
    draw_montage_axes: bool,
) -> dict[str, Any]:
    """Sample and process one view or one time-aligned merged view set."""
    time_window = _effective_time_window(
        infos=infos,
        primary_view=primary_view,
        frame_start=frame_start,
        frame_end=frame_end,
        max_time=max_time,
    )
    primary_info = infos[primary_view]
    primary_indices, timestamps = compute_sample_timestamps(
        original_fps=float(primary_info["fps"]),
        frame_count=int(primary_info["frame_count"]),
        target_fps=fps,
        max_frames=max_frames,
        min_api_frames=min_api_frames,
        frame_start=int(time_window["effective_frame_start"]),
        frame_end=int(time_window["effective_frame_end"]),
    )
    if not primary_indices:
        raise VideoProcessError(f"no frames sampled from video view {primary_view!r}")

    frames, groups = _build_timepoint_frames(
        view_map,
        infos,
        primary_view,
        primary_indices,
        timestamps,
        resize_width=resize_width,
        draw_timestamps=draw_timestamps,
        draw_view_names=draw_view_names,
        merge_views=merge_views,
    )
    effective_merge_length = 0
    if merge_mode == "timeline_grid":
        effective_merge_length = len(frames) if merge_length < 1 else merge_length
    frames, groups = _apply_temporal_merge(
        frames,
        groups,
        merge_mode=merge_mode,
        merge_length=merge_length,
        draw_montage_axes=draw_montage_axes and merge_views,
    )
    return {
        "frames": frames,
        "frame_groups": groups,
        "sampled_frame_indices": primary_indices,
        "sampled_timestamps": timestamps,
        "num_sampled_frames": len(primary_indices),
        "effective_merge_length": effective_merge_length,
        "time_window": time_window,
    }


def _frame_message_tag(frame_group: dict[str, Any]) -> dict[str, Any]:
    tags: list[str] = []
    for timestamp in _ordered_unique(list(frame_group.get("timestamps", []))):
        try:
            tags.append(f"<t={float(timestamp):.2f}s>")
        except (TypeError, ValueError):
            tags.append(f"<t={timestamp}s>")
    for view_name in _ordered_unique(list(frame_group.get("views", []))):
        tags.append(f"<{view_name}>")
    return {"type": "text", "text": " ".join(tags)}


def _encode_processed_media(
    frames: list[np.ndarray],
    frame_groups: list[dict[str, Any]],
    *,
    input_mode: str,
    jpeg_quality: int,
    target_fps: float,
    add_frame_tags: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bytes | None, dict[str, Any] | None]:
    if input_mode == "image_sequence":
        parts: list[dict[str, Any]] = []
        message_groups: list[dict[str, Any]] = []
        for frame, frame_group in zip(frames, frame_groups, strict=True):
            message_group = dict(frame_group)
            if add_frame_tags:
                message_group["message_tag_part_index"] = len(parts)
                parts.append(_frame_message_tag(frame_group))
            message_group["message_media_part_index"] = len(parts)
            parts.append(encode_frame_to_image_part(frame, jpeg_quality))
            message_groups.append(message_group)
        return parts, message_groups, None, None

    video_timestamps = [float(group.get("timestamps", [idx])[0]) for idx, group in enumerate(frame_groups)]
    part, payload, encoding_meta = encode_frames_to_video_part(
        frames,
        timestamps=video_timestamps,
        fallback_fps=target_fps,
    )
    all_timestamps = [timestamp for group in frame_groups for timestamp in group.get("timestamps", [])]
    all_frame_indices = [
        frame_index
        for group in frame_groups
        for frame_index in group.get("frame_indices", group.get("primary_frame_indices", []))
    ]
    video_group = {
        "output_index": 0,
        "message_media_part_index": 0,
        "source_processed_frame_indices": list(range(len(frames))),
        "frame_indices": all_frame_indices,
        "timestamps": all_timestamps,
        "views": _ordered_unique([view for group in frame_groups for view in group.get("views", [])]),
    }
    return [part], [video_group], payload, encoding_meta


def _view_message_label(view_name: str, input_mode: str) -> dict[str, Any]:
    media_name = "图像序列" if input_mode == "image_sequence" else "视频"
    return {
        "type": "text",
        "text": f"以下视觉输入属于视角 {view_name}，是该视角独立处理后的{media_name}。",
    }


def build_video_inputs(
    video_path: str | Path | list[str | Path] | dict[str, str | Path],
    *,
    fps: float,
    max_frames: int,
    resize_width: int,
    jpeg_quality: int,
    max_time: float | int = -1,
    draw_timestamps: bool = True,
    draw_view_names: bool = True,
    min_api_frames: int = 1,
    frame_start: int = 0,
    frame_end: int | None = None,
    merge_views: bool = False,
    merge_mode: str = "per_frame",
    merge_length: int = 0,
    draw_montage_axes: bool = False,
    view_names: list[str] | None = None,
    input_mode: str = "image_sequence",
    add_frame_tags: bool = False,
    save_processed_path: str | Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build OpenAI-compatible image parts and video metadata.

    Args:
        video_path: Single video path, list of paths, or dict mapping view names to paths.
        fps: Target sampling FPS on the original time axis.
        max_frames: Maximum sampled timepoints before temporal montage.
        max_time: Maximum original-video duration in seconds to process for this stage.
            ``-1`` disables this limit; values greater than 0 limit processing to
            ``0 <= timestamp < min(max_time, original_duration)`` before sampling.
        resize_width: Per-view resize width before stitching. Aspect ratio is preserved.
        jpeg_quality: JPEG quality in [1, 100]. Lower values reduce payload size but lose detail.
        draw_timestamps: Overlay timestamps before encoding/merging.
        draw_view_names: Overlay view names before encoding/merging. This replaces the legacy
            ``draw_viewposition`` name.
        min_api_frames: Minimum sampled timepoints when enough source frames exist.
        frame_start/frame_end: Inclusive source-frame range on the primary view.
        merge_views: If true, stitch all selected views vertically at each sampled timestamp.
            If false, preserve the old behavior for one selected view; multiple selected views
            are sampled, processed, and encoded independently.
        merge_mode: ``per_frame`` emits one image per sampled timestamp; ``timeline_grid``
            merges sampled timestamps into left-to-right timeline montages.
        merge_length: In ``timeline_grid`` mode, merge this many consecutive processed
            frames. Values < 1 merge all sampled timestamps into one montage.
        draw_montage_axes: If true, add timestamp column labels and view row labels
            after multi-view timeline montage generation. Only applies when
            ``merge_views`` is true and the effective timeline length is greater than 1.
        view_names: Selected/ordered views. This replaces the legacy ``merge_view_names`` name.
        input_mode: ``image_sequence`` emits JPEG image parts; ``video`` encodes processed
            frames into MP4 ``video_url`` parts.
        add_frame_tags: If true in ``image_sequence`` mode, insert a text part before every
            image using ``<t=1.25s> <view_name>`` tags. It has no effect in ``video`` mode.
        save_processed_path: Optional directory for final processed media and ``video_meta.json``.

    Returns:
        ``(media_parts, video_meta)``. For independent multi-view input, ``media_parts`` also
        contains text parts that identify the view owning each following media group.
    """
    max_time = _normalize_max_time(max_time)
    _validate_video_config(
        fps=fps,
        max_frames=max_frames,
        max_time=max_time,
        resize_width=resize_width,
        input_mode=input_mode,
        merge_mode=merge_mode,
        min_api_frames=min_api_frames,
    )
    jpeg_quality = _normalize_jpeg_quality(jpeg_quality)
    view_map, input_type = normalize_video_input(video_path, view_names=view_names)
    infos = {view_name: read_video_info(path) for view_name, path in view_map.items()}
    primary_view = next(iter(view_map.keys()))
    primary_info = infos[primary_view]
    requested_merge_length = int(merge_length or 0)
    separate_view_inputs = not bool(merge_views) and len(view_map) > 1
    frame_tags_enabled = bool(add_frame_tags) and input_mode == "image_sequence"
    frames_by_view: dict[str, list[np.ndarray]] | None = None
    video_payloads: dict[str, bytes] = {}
    view_outputs: dict[str, dict[str, Any]] = {}
    top_video_encoding: dict[str, Any] | None = None

    if separate_view_inputs:
        parts: list[dict[str, Any]] = []
        frames = []
        groups: list[dict[str, Any]] = []
        frames_by_view = {}
        media_part_count = 0

        for view_name, path in view_map.items():
            result = _process_view_set(
                {view_name: path},
                {view_name: infos[view_name]},
                view_name,
                fps=float(fps),
                max_frames=int(max_frames),
                max_time=max_time,
                resize_width=int(resize_width),
                draw_timestamps=bool(draw_timestamps),
                draw_view_names=bool(draw_view_names),
                min_api_frames=int(min_api_frames),
                frame_start=int(frame_start),
                frame_end=frame_end,
                merge_views=False,
                merge_mode=merge_mode,
                merge_length=requested_merge_length,
                draw_montage_axes=False,
            )
            media_parts, local_groups, payload, encoding_meta = _encode_processed_media(
                result["frames"],
                result["frame_groups"],
                input_mode=input_mode,
                jpeg_quality=jpeg_quality,
                target_fps=float(fps),
                add_frame_tags=frame_tags_enabled,
            )
            label_part_index = len(parts)
            parts.append(_view_message_label(view_name, input_mode))
            media_start_part_index = len(parts)
            parts.extend(media_parts)

            global_groups: list[dict[str, Any]] = []
            for local_group in local_groups:
                global_group = dict(local_group)
                local_output_index = int(local_group.get("output_index", len(global_groups)))
                global_group["view_name"] = view_name
                global_group["view_output_index"] = local_output_index
                global_group["output_index"] = media_part_count + local_output_index
                for index_key in ("message_tag_part_index", "message_media_part_index"):
                    if index_key in global_group:
                        global_group[index_key] = media_start_part_index + int(global_group[index_key])
                global_groups.append(global_group)
            groups.extend(global_groups)

            frames_by_view[view_name] = result["frames"]
            if payload is not None:
                video_payloads[view_name] = payload
            view_outputs[view_name] = {
                "video_info": infos[view_name],
                "original_fps": float(infos[view_name]["fps"]),
                "target_fps": float(fps),
                **result["time_window"],
                "sampled_frame_indices": result["sampled_frame_indices"],
                "sampled_timestamps": result["sampled_timestamps"],
                "num_sampled_frames": result["num_sampled_frames"],
                "num_output_parts": len(local_groups),
                "num_message_parts": len(media_parts),
                "message_label_part_index": label_part_index,
                "message_media_start_part_index": media_start_part_index,
                "effective_merge_length": result["effective_merge_length"],
                "output_groups": local_groups,
                "video_encoding": encoding_meta,
            }
            media_part_count += len(local_groups)

        primary_result = view_outputs[primary_view]
        time_window = {
            key: primary_result[key]
            for key in (
                "original_duration",
                "configured_max_time",
                "effective_duration",
                "effective_start_time",
                "effective_end_time",
                "effective_frame_start",
                "effective_frame_end",
                "was_time_limited",
                "was_limited_by_frame_range",
            )
        }
        time_window.update(
            {
                "min_input_duration": min(float(info["duration"]) for info in infos.values()),
                "per_view_durations": {name: float(info["duration"]) for name, info in infos.items()},
                "per_view_effective_durations": {
                    name: float(output["effective_duration"]) for name, output in view_outputs.items()
                },
                "was_limited_by_view_duration": False,
                "was_any_view_time_limited": any(
                    bool(output["was_time_limited"]) for output in view_outputs.values()
                ),
            }
        )
        primary_indices = list(primary_result["sampled_frame_indices"])
        timestamps = list(primary_result["sampled_timestamps"])
        effective_merge_length = int(primary_result["effective_merge_length"])
        num_output_parts = media_part_count
        processed_layout = "per_view_directories"
    else:
        result = _process_view_set(
            view_map,
            infos,
            primary_view,
            fps=float(fps),
            max_frames=int(max_frames),
            max_time=max_time,
            resize_width=int(resize_width),
            draw_timestamps=bool(draw_timestamps),
            draw_view_names=bool(draw_view_names),
            min_api_frames=int(min_api_frames),
            frame_start=int(frame_start),
            frame_end=frame_end,
            merge_views=bool(merge_views),
            merge_mode=merge_mode,
            merge_length=requested_merge_length,
            draw_montage_axes=bool(draw_montage_axes),
        )
        frames = result["frames"]
        parts, groups, payload, encoding_meta = _encode_processed_media(
            frames,
            result["frame_groups"],
            input_mode=input_mode,
            jpeg_quality=jpeg_quality,
            target_fps=float(fps),
            add_frame_tags=frame_tags_enabled,
        )
        if payload is not None:
            video_payloads["__combined__"] = payload
        top_video_encoding = encoding_meta
        time_window = result["time_window"]
        primary_indices = result["sampled_frame_indices"]
        timestamps = result["sampled_timestamps"]
        effective_merge_length = result["effective_merge_length"]
        num_output_parts = len(groups)
        processed_layout = "flat"

    video_meta: dict[str, Any] = {
        "input_type": input_type,
        "input_mode": input_mode,
        "view_names": list(view_map.keys()),
        "primary_view": primary_view,
        "video_info": infos,
        "original_fps": float(primary_info["fps"]),
        "target_fps": float(fps),
        "frame_start": int(frame_start),
        "frame_end": frame_end,
        **time_window,
        "sampled_frame_indices": primary_indices,
        "sampled_timestamps": timestamps,
        "num_sampled_frames": len(primary_indices),
        "num_output_parts": num_output_parts,
        "num_message_parts": len(parts),
        "resize_width": int(resize_width),
        "jpeg_quality": jpeg_quality,
        "draw_timestamps": bool(draw_timestamps),
        "draw_view_names": bool(draw_view_names),
        "merge_views": bool(merge_views),
        "merge_mode": merge_mode,
        "merge_length": requested_merge_length,
        "effective_merge_length": effective_merge_length,
        "draw_montage_axes": bool(draw_montage_axes),
        "separate_view_inputs": separate_view_inputs,
        "message_view_labels": separate_view_inputs,
        "add_frame_tags_requested": bool(add_frame_tags),
        "add_frame_tags": frame_tags_enabled,
        "processed_layout": processed_layout,
        "processed_view_directories": {
            name: _safe_view_dir_name(name) for name in view_map
        } if separate_view_inputs else {},
        "view_outputs": view_outputs,
        "video_encoding": top_video_encoding,
        "output_groups": groups,
    }

    if save_processed_path:
        save_processed_frames(
            frames,
            video_meta,
            save_processed_path,
            frames_by_view=frames_by_view,
            video_payloads=video_payloads,
        )

    return parts, video_meta
