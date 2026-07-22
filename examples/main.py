"""Batch prediction entrypoint for robot_mind2 task JSON.

Default usage from the repository root:

    python examples/main.py

For a quick pipeline check without calling the VLM:

    python examples/main.py --dry-run --limit 1

Per-task outputs are saved under ``examples/vlm_annotation_batch_predictions``.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import yaml


try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass


SCRIPT_DIR = Path(__file__).resolve().parent
PACKAGE_DIR = SCRIPT_DIR.parent
PACKAGE_PARENT = PACKAGE_DIR.parent
PACKAGE_NAME = PACKAGE_DIR.name
DEFAULT_TASKS_PATH = SCRIPT_DIR / "data" / "robocoin_view_combos_max20_local_paths_preferred_head_view_subtask_splits.json"


def _ensure_imports() -> Any:
    """Import pipeline helpers from the current package directory name."""
    if str(PACKAGE_PARENT) not in sys.path:
        sys.path.insert(0, str(PACKAGE_PARENT))

    pipeline_module = importlib.import_module(f"{PACKAGE_NAME}.pipeline")
    return pipeline_module.run_pipeline


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, list):
        raise ValueError(f"Task file must contain a JSON list: {path}")
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Task item #{index} must be a JSON object")
    return data


def _safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(value))
    value = re.sub(r"\s+", "_", value).strip(" ._")
    return value or "run"


def _infer_dataset_name(video_path: Any) -> str:
    if isinstance(video_path, dict) and video_path:
        first_path = next(iter(video_path.values()))
    elif isinstance(video_path, (list, tuple)) and video_path:
        first_path = video_path[0]
    else:
        first_path = video_path

    parts = Path(str(first_path)).parts
    if "videos" in parts:
        idx = parts.index("videos")
        if idx > 0:
            return parts[idx - 1]
    return "task"


def _resolve_video_path(value: Any, base_dir: Path) -> Any:
    if isinstance(value, dict):
        return {name: _resolve_video_path(path, base_dir) for name, path in value.items()}
    if isinstance(value, list):
        return [_resolve_video_path(path, base_dir) for path in value]
    if isinstance(value, tuple):
        return [_resolve_video_path(path, base_dir) for path in value]

    path = Path(str(value))
    if path.is_absolute():
        return str(path)
    return str((base_dir / path).resolve())


def _format_annotation_subtasks(subtasks: Any) -> str:
    if not isinstance(subtasks, list):
        raise ValueError("annotation subtasks must be a list")

    lines = ["["]
    for index, subtask in enumerate(subtasks):
        if not isinstance(subtask, dict):
            raise ValueError(f"annotation subtask #{index} must be a JSON object")
        comma = "," if index < len(subtasks) - 1 else ""
        subtask_id = subtask.get("subtask_id")
        subtask_text = subtask.get("subtask")
        start_time = subtask.get("start_time")
        end_time = subtask.get("end_time")
        start_text = "null" if start_time is None else f"{float(start_time):.2f}"
        end_text = "null" if end_time is None else f"{float(end_time):.2f}"
        lines.extend(
            [
                "  {",
                f"    \"subtask_id\": {json.dumps(subtask_id, ensure_ascii=False)},",
                f"    \"subtask\": {json.dumps(subtask_text, ensure_ascii=False)},",
                f"    \"start_time\": {start_text},",
                f"    \"end_time\": {end_text}",
                f"  }}{comma}",
            ]
        )
    lines.append("]")
    return "\n".join(lines)


def _extract_scene_annotation(scenes: Any) -> str:
    if not isinstance(scenes, list) or not scenes:
        return ""
    first_scene = scenes[0]
    if not isinstance(first_scene, dict):
        return ""
    return str(first_scene.get("scene_annotation") or "")


def _slice_tasks(tasks: list[dict[str, Any]], *, start_index: int, limit: int | None) -> list[tuple[int, dict[str, Any]]]:
    end_index = len(tasks) if limit is None else min(len(tasks), start_index + limit)
    return list(enumerate(tasks[start_index:end_index], start=start_index))


def _build_context(item: dict[str, Any], index: int, *, task_base_dir: Path) -> dict[str, Any]:
    if "video_path" not in item:
        raise ValueError(f"Task item #{index} missing 'video_path'")
    instruction = item.get("task") or item.get("instruction")
    if not instruction:
        raise ValueError(f"Task item #{index} missing 'task' or 'instruction'")

    episode_id = str(item.get("episode_id") or f"item_{index:05d}")
    return {
        "input": {
            "video_path": _resolve_video_path(item["video_path"], task_base_dir),
            "instruction": str(instruction),
            "task": str(instruction),
            "scene_annotation": _extract_scene_annotation(item.get("scenes")),
            "subtasks": _format_annotation_subtasks(item.get("subtasks", [])),
            "video_id": episode_id,
            "task_index": index,
        },
        "stages": {},
    }


def _selected_output_json_paths(context: dict[str, Any]) -> list[dict[str, str]]:
    run_dir = context.get("run_dir")
    if not run_dir:
        raise RuntimeError("run_dir was not set; stage output.json cannot be verified")
    stage_names = context.get("pipeline", {}).get("selected_stages") or []
    if not stage_names:
        raise RuntimeError("selected_stages was not set; stage output.json cannot be verified")

    outputs: list[dict[str, str]] = []
    missing: list[str] = []
    for stage_name in stage_names:
        output_path = Path(run_dir) / "stages" / str(stage_name) / "output.json"
        if output_path.exists():
            outputs.append({"stage": str(stage_name), "output_json": str(output_path)})
        else:
            missing.append(str(output_path))
    if missing:
        raise RuntimeError(f"stage output.json was not saved: {missing}")
    return outputs

def _run_one_task_worker(
    *,
    item: dict[str, Any],
    index: int,
    total_tasks: int,
    task_base_dir: str,
    config: dict[str, Any],
    output_dir: str,
    run_options: dict[str, Any],
) -> dict[str, Any]:
    """Run one episode pipeline in a worker process."""
    run_pipeline = _ensure_imports()
    started_at = time.perf_counter()

    dataset_name = _infer_dataset_name(item.get("video_path"))
    episode_id = str(item.get("episode_id") or f"item_{index:05d}")
    run_name = _safe_name(f"{index:05d}_{dataset_name}_{episode_id}")
    context = _build_context(item, index, task_base_dir=Path(task_base_dir))

    run_pipeline(
        context,
        config,
        dry_run=bool(run_options.get("dry_run", False)),
        start_from=run_options.get("start_from"),
        stop_after=run_options.get("stop_after"),
        skip_existing=bool(run_options.get("skip_existing", False)),
        output_dir=Path(output_dir),
        run_name=run_name,
        save_results=True,
    )
    output_json_paths = _selected_output_json_paths(context)

    return {
        "index": index,
        "total_tasks": total_tasks,
        "episode_id": episode_id,
        "run_name": run_name,
        "run_dir": context.get("run_dir"),
        "output_json_paths": output_json_paths,
        "elapsed_seconds": time.perf_counter() - started_at,
    }


def _success_record(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "episode_id": result.get("episode_id"),
        "index": result.get("index"),
        "run_name": result.get("run_name"),
        "run_dir": result.get("run_dir"),
        "output_json_paths": result.get("output_json_paths") or [],
        "elapsed_seconds": result.get("elapsed_seconds"),
    }


def _failure_record(index: int, item: dict[str, Any], error_text: str) -> dict[str, Any]:
    dataset_name = _infer_dataset_name(item.get("video_path"))
    episode_id = str(item.get("episode_id") or f"item_{index:05d}")
    return {
        "episode_id": episode_id,
        "index": index,
        "run_name": _safe_name(f"{index:05d}_{dataset_name}_{episode_id}"),
        "error": error_text,
    }


def _save_batch_status(output_dir: Path, successful: list[dict[str, Any]], failed: list[dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "successful_episodes.json").write_text(
        json.dumps(successful, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (output_dir / "failed_episodes.json").write_text(
        json.dumps(failed, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _handle_task_success(result: dict[str, Any]) -> None:
    elapsed_seconds = result.get("elapsed_seconds")
    if elapsed_seconds is not None:
        print(f"  elapsed -> {float(elapsed_seconds):.2f}s")
    output_paths = result.get("output_json_paths") or []
    if output_paths:
        output_summary = ", ".join(str(item.get("output_json")) for item in output_paths if isinstance(item, dict))
        print(f"  output.json -> {output_summary}")
    print(f"  ok -> {result.get('run_dir')}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch run configured VLM pipeline on robot_mind2 tasks.")
    parser.add_argument("--config", default=str(PACKAGE_DIR / "config" / "config_annotation.yaml"), help="Path to config.yaml.")
    parser.add_argument(
        "--tasks",
        default=str(DEFAULT_TASKS_PATH),
        help="Path to task JSON list.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(SCRIPT_DIR / "vlm_annotation_batch_predictions"),
        help="Directory for per-task outputs.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Run video/prompt pipeline without real model calls.")
    parser.add_argument("--start-index", type=int, default=0, help="Start task index, inclusive.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of tasks to process.")
    parser.add_argument("--start-from", default=None, help="Optional pipeline start stage.")
    parser.add_argument("--stop-after", default=None, help="Optional pipeline stop stage.")
    parser.add_argument("--skip-existing", action="store_true", help="Skip stages already present in context.")
    parser.add_argument("--workers", type=int, default=4, help="Number of episode processes to run concurrently.")
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop on first failed task instead of writing an error record and continuing.",
    )
    return parser.parse_args()


def main() -> None:
    total_started_at = time.perf_counter()
    args = parse_args()
    if args.start_index < 0:
        raise ValueError("--start-index must be >= 0")
    if args.limit is not None and args.limit < 0:
        raise ValueError("--limit must be >= 0")
    if args.workers < 1:
        raise ValueError("--workers must be >= 1")

    _ensure_imports()

    config_path = Path(args.config)
    tasks_path = Path(args.tasks)
    output_dir = Path(args.output_dir)

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    tasks = _load_json_list(tasks_path)
    task_base_dir = tasks_path.resolve().parent
    selected_tasks = _slice_tasks(tasks, start_index=args.start_index, limit=args.limit)

    output_dir.mkdir(parents=True, exist_ok=True)
    successful_episodes: list[dict[str, Any]] = []
    failed_episodes: list[dict[str, Any]] = []
    _save_batch_status(output_dir, successful_episodes, failed_episodes)
    ok_count = 0
    error_count = 0

    print(f"Loaded config: {config_path}")
    print(f"Loaded tasks: {tasks_path} ({len(tasks)} total, {len(selected_tasks)} selected)")
    print(f"Saving per-task outputs to: {output_dir}")
    print(f"Workers: {args.workers}")

    run_options = {
        "dry_run": args.dry_run,
        "start_from": args.start_from,
        "stop_after": args.stop_after,
        "skip_existing": args.skip_existing,
    }

    def print_task_header(index: int, item: dict[str, Any]) -> None:
        dataset_name = _infer_dataset_name(item.get("video_path"))
        episode_id = str(item.get("episode_id") or f"item_{index:05d}")
        run_name = _safe_name(f"{index:05d}_{dataset_name}_{episode_id}")
        print(f"\n[{index + 1}/{len(tasks)}] {run_name}")

    def build_worker_kwargs(index: int, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "item": item,
            "index": index,
            "total_tasks": len(tasks),
            "task_base_dir": str(task_base_dir),
            "config": config,
            "output_dir": str(output_dir),
            "run_options": run_options,
        }

    if args.workers == 1:
        for index, item in selected_tasks:
            print_task_header(index, item)
            try:
                result = _run_one_task_worker(**build_worker_kwargs(index, item))
                _handle_task_success(result)
                successful_episodes.append(_success_record(result))
                _save_batch_status(output_dir, successful_episodes, failed_episodes)
                ok_count += 1
            except Exception as exc:
                error_text = f"{type(exc).__name__}: {exc}"
                error_count += 1
                print(f"  error -> {error_text}")
                failed_episodes.append(_failure_record(index, item, error_text))
                _save_batch_status(output_dir, successful_episodes, failed_episodes)
                if args.fail_fast:
                    traceback.print_exc()
                    raise
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            future_to_task = {
                executor.submit(
                    _run_one_task_worker,
                    **build_worker_kwargs(index, item),
                ): (index, item)
                for index, item in selected_tasks
            }
            for future in as_completed(future_to_task):
                index, item = future_to_task[future]
                print_task_header(index, item)
                try:
                    result = future.result()
                    _handle_task_success(result)
                    successful_episodes.append(_success_record(result))
                    _save_batch_status(output_dir, successful_episodes, failed_episodes)
                    ok_count += 1
                except Exception as exc:
                    error_text = f"{type(exc).__name__}: {exc}"
                    error_count += 1
                    print(f"  error -> {error_text}")
                    failed_episodes.append(_failure_record(index, item, error_text))
                    _save_batch_status(output_dir, successful_episodes, failed_episodes)
                    if args.fail_fast:
                        for pending in future_to_task:
                            pending.cancel()
                        traceback.print_exc()
                        raise

    total_elapsed = time.perf_counter() - total_started_at
    processed_count = ok_count + error_count
    average_elapsed = total_elapsed / processed_count if processed_count else 0.0
    print(
        f"\nDone. ok={ok_count}, error={error_count}, "
        f"total_elapsed={total_elapsed:.2f}s, avg_per_episode={average_elapsed:.2f}s"
    )


if __name__ == "__main__":
    main()
