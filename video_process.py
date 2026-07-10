"""Self-contained lightweight video preprocessing for VLM stage experiments.

This module has no dependency on the original VLM Auto Annotation codebase.
It converts a single video or time-aligned multi-view videos into
OpenAI-compatible ``image_url`` message parts and returns sampling metadata.
"""

from __future__ import annotations

import base64
import json
import math
from pathlib import Path
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
    if input_mode not in {"image_sequence", "video"}:
        raise VideoProcessError(f"input_mode must be 'image_sequence' or 'video', got {input_mode!r}")
    if merge_mode not in {"per_frame", "timeline_grid"}:
        raise VideoProcessError(f"merge_mode must be 'per_frame' or 'timeline_grid', got {merge_mode!r}")


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


def save_processed_frames(frames: list[np.ndarray], video_meta: dict[str, Any], save_processed_path: str | Path) -> None:
    """Save processed frames and ``video_meta.json`` for debugging."""
    out_dir = Path(save_processed_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old_frame in out_dir.glob("frame_*.jpg"):
        old_frame.unlink()
    for idx, frame in enumerate(frames):
        ok = cv2.imwrite(str(out_dir / f"frame_{idx:06d}.jpg"), frame)
        if not ok:
            raise VideoProcessError(f"failed to save processed frame {idx} to {out_dir}")
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


def build_video_inputs(
    video_path: str | Path | list[str | Path] | dict[str, str | Path],
    *,
    fps: float,
    max_frames: int,
    resize_width: int,
    jpeg_quality: int,
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
    save_processed_path: str | Path | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build OpenAI-compatible image parts and video metadata.

    Args:
        video_path: Single video path, list of paths, or dict mapping view names to paths.
        fps: Target sampling FPS on the original time axis.
        max_frames: Maximum sampled timepoints before temporal montage.
        resize_width: Per-view resize width before stitching. Aspect ratio is preserved.
        jpeg_quality: JPEG quality in [1, 100]. Lower values reduce payload size but lose detail.
        draw_timestamps: Overlay timestamps before encoding/merging.
        draw_view_names: Overlay view names before encoding/merging. This replaces the legacy
            ``draw_viewposition`` name.
        min_api_frames: Minimum sampled timepoints when enough source frames exist.
        frame_start/frame_end: Inclusive source-frame range on the primary view.
        merge_views: If true, stitch all selected views vertically at each sampled timestamp.
            If false, output only the primary view.
        merge_mode: ``per_frame`` emits one image per sampled timestamp; ``timeline_grid``
            merges sampled timestamps into left-to-right timeline montages.
        merge_length: In ``timeline_grid`` mode, merge this many consecutive processed
            frames. Values < 1 merge all sampled timestamps into one montage.
        draw_montage_axes: If true, add timestamp column labels and view row labels
            after multi-view timeline montage generation. Only applies when
            ``merge_views`` is true and the effective timeline length is greater than 1.
        view_names: Selected/ordered views. This replaces the legacy ``merge_view_names`` name.
        input_mode: ``image_sequence`` is implemented. ``video`` raises a clear error.
        save_processed_path: Optional directory for final processed images and ``video_meta.json``.

    Returns:
        ``(image_parts, video_meta)``.
    """
    _validate_video_config(
        fps=fps,
        max_frames=max_frames,
        resize_width=resize_width,
        input_mode=input_mode,
        merge_mode=merge_mode,
        min_api_frames=min_api_frames,
    )
    jpeg_quality = _normalize_jpeg_quality(jpeg_quality)
    if input_mode == "video":
        raise VideoProcessError(
            "input_mode='video' is intentionally not implemented in the standalone refactor. "
            "Use input_mode='image_sequence'."
        )

    view_map, input_type = normalize_video_input(video_path, view_names=view_names)
    infos = {view_name: read_video_info(path) for view_name, path in view_map.items()}
    primary_view = next(iter(view_map.keys()))
    primary_info = infos[primary_view]

    primary_indices, timestamps = compute_sample_timestamps(
        original_fps=float(primary_info["fps"]),
        frame_count=int(primary_info["frame_count"]),
        target_fps=float(fps),
        max_frames=int(max_frames),
        min_api_frames=int(min_api_frames),
        frame_start=int(frame_start),
        frame_end=frame_end,
    )
    if not primary_indices:
        raise VideoProcessError("no frames sampled from video input")

    frames, groups = _build_timepoint_frames(
        view_map,
        infos,
        primary_view,
        primary_indices,
        timestamps,
        resize_width=int(resize_width),
        draw_timestamps=bool(draw_timestamps),
        draw_view_names=bool(draw_view_names),
        merge_views=bool(merge_views),
    )
    requested_merge_length = int(merge_length or 0)
    effective_merge_length = 0
    if merge_mode == "timeline_grid":
        effective_merge_length = len(frames) if requested_merge_length < 1 else requested_merge_length
    frames, groups = _apply_temporal_merge(
        frames,
        groups,
        merge_mode=merge_mode,
        merge_length=requested_merge_length,
        draw_montage_axes=bool(draw_montage_axes) and bool(merge_views),
    )
    parts = [encode_frame_to_image_part(frame, jpeg_quality) for frame in frames]

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
        "sampled_frame_indices": primary_indices,
        "sampled_timestamps": timestamps,
        "num_sampled_frames": len(primary_indices),
        "num_output_parts": len(parts),
        "resize_width": int(resize_width),
        "jpeg_quality": jpeg_quality,
        "draw_timestamps": bool(draw_timestamps),
        "draw_view_names": bool(draw_view_names),
        "merge_views": bool(merge_views),
        "merge_mode": merge_mode,
        "merge_length": requested_merge_length,
        "effective_merge_length": effective_merge_length,
        "draw_montage_axes": bool(draw_montage_axes),
        "output_groups": groups,
    }

    if save_processed_path:
        save_processed_frames(frames, video_meta, save_processed_path)

    return parts, video_meta
