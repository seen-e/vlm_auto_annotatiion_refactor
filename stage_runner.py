"""Run one configured lightweight VLM stage."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .json_utils import extract_json
from .model_client import call_vlm
from .prompt_utils import load_stage_prompt, render_template, resolve_input_fields
from .video_process import build_video_inputs


class StageRunnerError(RuntimeError):
    """Raised when a stage fails to run."""


_VIDEO_SAVE_KEYS = {"save_processed", "processed_output_path", "output_path", "save_processed_path"}


def _model_cfg(config: dict[str, Any], stage_cfg: dict[str, Any]) -> dict[str, Any]:
    model_cfg = dict(config.get("model") or {})
    generation = dict(stage_cfg.get("generation") or {})
    model_cfg.update(generation)
    return model_cfg


def _dry_run_output(stage_name: str) -> dict[str, Any]:
    if stage_name == "scene":
        return {
            "scene_summary": "dry_run scene output",
            "executors": [{"executor_id": "single", "description": "dry-run executor"}],
            "touched_objects": [{"object_id": "object", "description": "dry-run object"}],
            "background_objects": [],
            "best_observation_views": [],
        }
    if stage_name == "analysis":
        return {
            "action_sequence": [
                {
                    "step_id": 1,
                    "executor": "single",
                    "action": "grasp",
                    "object": "object",
                    "evidence": "dry-run evidence",
                    "confidence": 0.0,
                }
            ]
        }
    if stage_name == "refinement":
        return {
            "refined_segments": [
                {
                    "step_id": 1,
                    "executor": "single",
                    "action": "grasp",
                    "object": "object",
                    "start_time": 0.0,
                    "end_time": 1.0,
                    "evidence": "dry-run evidence",
                    "confidence": 0.0,
                }
            ]
        }
    return {"stage": stage_name, "status": "dry_run_ok"}


def _ensure_context(context: dict[str, Any]) -> None:
    context.setdefault("input", {})
    context.setdefault("stages", {})
    if "video_path" not in context["input"]:
        raise StageRunnerError("context['input']['video_path'] is required")


def _safe_path_name(value: Any, *, default: str) -> str:
    invalid = '<>:"/\\|?*'
    text = str(value or default)
    cleaned = "".join("_" if ch in invalid or ord(ch) < 32 else ch for ch in text)
    return cleaned.strip(" .") or default


def _resolve_processed_output_path(
    *,
    stage_name: str,
    context: dict[str, Any],
    video_cfg: dict[str, Any],
    run_dir: str | Path | None,
) -> Path | None:
    if not bool(video_cfg.get("save_processed", False)):
        return None

    configured_root = video_cfg.get("processed_output_path") or video_cfg.get("output_path") or video_cfg.get("save_processed_path")
    if configured_root:
        root = Path(str(configured_root))
    elif run_dir is not None:
        root = Path(run_dir).parent
    else:
        root = Path("outputs")

    input_ctx = context.get("input", {})
    episode_id = input_ctx.get("episode_id") or input_ctx.get("video_id") or input_ctx.get("task_index") or "episode"
    return root / _safe_path_name(episode_id, default="episode") / _safe_path_name(stage_name, default="stage")


def build_video_layout_description(video_cfg: dict[str, Any], video_meta: dict[str, Any]) -> str:
    """Build a prompt-facing description of the actual visual input layout."""
    view_names = [str(name) for name in video_meta.get("view_names") or video_cfg.get("view_names") or []]
    merge_views = bool(video_meta.get("merge_views", video_cfg.get("merge_views", False)))
    merge_mode = str(video_meta.get("merge_mode", video_cfg.get("merge_mode", "per_frame")))
    merge_length = int(video_meta.get("merge_length", video_cfg.get("merge_length", 0)) or 0)
    draw_timestamps = bool(video_meta.get("draw_timestamps", video_cfg.get("draw_timestamps", True)))
    draw_view_names = bool(video_meta.get("draw_view_names", video_cfg.get("draw_view_names", True)))
    sampled_count = video_meta.get("num_sampled_frames")
    output_count = video_meta.get("num_output_parts")
    primary_view = video_meta.get("primary_view")

    lines = [
        "当前视频输入布局说明：",
        f"- 输入模式：{video_meta.get('input_mode', video_cfg.get('input_mode', 'image_sequence'))}。",
    ]
    if sampled_count is not None and output_count is not None:
        lines.append(f"- 共采样 {sampled_count} 个主时间点，最终送入模型 {output_count} 张图像。")
    if view_names:
        lines.append(f"- 视角顺序：{', '.join(view_names)}。")
    if primary_view:
        lines.append(f"- 主时间轴基于视角 {primary_view}；其他视角按同一时间戳对齐。")

    if merge_views and len(view_names) > 1:
        lines.append("- 同一时间点的多个视角会先横向拼接到同一张图中。")
        lines.append("- 横向相邻通常表示不同摄像机视角，不表示真实世界中物体一定左右相邻。")
    elif len(view_names) > 1:
        lines.append("- 多个视角不会合并到同一张图；每张图通常只对应一个视角和一个时间点。")
    else:
        lines.append("- 每个采样时间点只有一个视角。")

    if merge_length > 1:
        lines.append(
            f"- 每 {merge_length} 个连续输出图像会进一步合成为一个时间网格/montage；"
            "网格按从左到右、从上到下表示时间推进。"
        )
    elif merge_mode == "timeline_grid":
        lines.append("- 当前配置选择 timeline_grid，但未启用多时间点合并；图像仍按单个时间点输出。")
    else:
        lines.append("- 未启用多时间点 montage；每张图像通常对应一个采样时间点。")

    if draw_timestamps:
        lines.append("- 图像上绘制了 t=...s 时间戳，动作顺序和边界判断应优先参考这些时间戳。")
    else:
        lines.append("- 图像上没有绘制时间戳，需要根据输入顺序和上下文估计时间推进。")
    if draw_view_names:
        lines.append("- 图像上绘制了视角名称，可用来区分不同摄像机来源。")
    elif len(view_names) > 1:
        lines.append("- 图像上没有绘制视角名称，需要按上述视角顺序理解不同视角来源。")

    return "\n".join(lines)


def run_stage(
    stage_name: str,
    context: dict[str, Any],
    config: dict[str, Any],
    *,
    dry_run: bool = False,
    run_dir: str | Path | None = None,
    save_result: bool = False,
) -> dict[str, Any]:
    """Run one configured VLM stage and store its result in context.

    The stage behavior is fully defined by ``config['stages'][stage_name]``.
    Prompt placeholders can directly reference ``ctx.*`` and ``prompt.*``.
    """
    try:
        _ensure_context(context)
        stages_cfg = config.get("stages") or {}
        if stage_name not in stages_cfg:
            raise StageRunnerError(f"stage {stage_name!r} not found in config['stages']")
        stage_cfg = stages_cfg[stage_name]
        video_cfg = dict(stage_cfg.get("video") or {})
        if not video_cfg:
            raise StageRunnerError(f"stage {stage_name!r} missing video config")
        processed_output_path = _resolve_processed_output_path(
            stage_name=stage_name,
            context=context,
            video_cfg=video_cfg,
            run_dir=run_dir,
        )
        video_build_cfg = {key: value for key, value in video_cfg.items() if key not in _VIDEO_SAVE_KEYS}
        if processed_output_path is not None:
            video_build_cfg["save_processed_path"] = processed_output_path

        image_parts, video_meta = build_video_inputs(context["input"]["video_path"], **video_build_cfg)
        if processed_output_path is not None:
            video_meta["processed_output_path"] = str(processed_output_path)
        current_video_layout = build_video_layout_description(video_cfg, video_meta)
        context["current_video_layout"] = current_video_layout
        extra_vars = resolve_input_fields(context, stage_cfg.get("input_fields"))
        system_template, user_template = load_stage_prompt(stage_cfg, stage_name=stage_name)
        system_prompt = render_template(system_template, context=context, extra_vars=extra_vars)
        user_prompt = render_template(user_template, context=context, extra_vars=extra_vars)

        usage: dict[str, Any] = {}
        if dry_run:
            parsed_json = _dry_run_output(stage_name)
            raw_text = json.dumps(parsed_json, ensure_ascii=False, indent=2)
        else:
            model_cfg = _model_cfg(config, stage_cfg)
            raw_text = call_vlm(
                base_url=str(model_cfg.get("base_url") or ""),
                api_key=str(model_cfg.get("api_key") or "EMPTY"),
                model=str(model_cfg.get("model") or model_cfg.get("name") or ""),
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                image_parts=image_parts,
                max_tokens=int(model_cfg.get("max_tokens", stage_cfg.get("max_tokens", 4096)) or 4096),
                temperature=float(model_cfg.get("temperature", 0.0) or 0.0),
                top_p=model_cfg.get("top_p"),
                top_k=model_cfg.get("top_k"),
                max_retries=int(model_cfg.get("max_retries", 3) or 3),
            )
            parsed_json = extract_json(raw_text)

        context["stages"][stage_name] = {
            "output": parsed_json,
            "raw_text": raw_text,
            "system_prompt": system_prompt,
            "prompt": user_prompt,
            "video_meta": video_meta,
            "video_layout": current_video_layout,
            "usage": usage,
        }

        if save_result and run_dir is not None:
            from .result_io import save_stage_result

            save_stage_result(context, stage_name, run_dir)
        return parsed_json
    except StageRunnerError:
        raise
    except Exception as exc:
        raise StageRunnerError(f"stage {stage_name!r} failed: {exc}") from exc
