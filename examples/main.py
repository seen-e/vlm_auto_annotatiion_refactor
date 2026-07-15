"""Batch prediction entrypoint for robot_mind2 task JSON.

Default usage from the repository root:

    python examples/main.py

For a quick pipeline check without calling the VLM:

    python examples/main.py --dry-run --limit 1

Per-task outputs are saved under ``examples/vlm_annotation_batch_predictions``.
After refinement completes, a standardized trajectory JSON is saved to each
task's ``trajectory_path`` when present, otherwise to ``--summary``.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
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
DEFAULT_TASKS_PATH = SCRIPT_DIR / "test_data" / "robogene_twoArm_franka_adjust_black_computer_stand.json"


def _ensure_imports() -> tuple[Any, Any, Any, Any, Any]:
    """Import pipeline helpers from the current package directory name."""
    if str(PACKAGE_PARENT) not in sys.path:
        sys.path.insert(0, str(PACKAGE_PARENT))

    pipeline_module = importlib.import_module(f"{PACKAGE_NAME}.pipeline")
    trajectory_module = importlib.import_module(f"{PACKAGE_NAME}.trajectory_summary")
    return (
        pipeline_module.run_pipeline,
        trajectory_module.build_trajectory,
        trajectory_module.resolve_summary_output_path,
        trajectory_module.save_trajectory,
        trajectory_module.register_unique_output_path,
    )


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
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
            "video_id": episode_id,
            "task_index": index,
        },
        "stages": {},
    }


def _refinement_record(context: dict[str, Any]) -> dict[str, Any] | None:
    record = context.get("stages", {}).get("refinement")
    return record if isinstance(record, dict) else None


def _run_one_task(
    *,
    item: dict[str, Any],
    index: int,
    total_tasks: int,
    task_base_dir: Path,
    config: dict[str, Any],
    output_dir: Path,
    summary_path: Path,
    args: argparse.Namespace,
    run_pipeline: Any,
    build_trajectory: Any,
    resolve_summary_output_path: Any,
) -> dict[str, Any]:
    dataset_name = _infer_dataset_name(item.get("video_path"))
    episode_id = str(item.get("episode_id") or f"item_{index:05d}")
    run_name = _safe_name(f"{index:05d}_{dataset_name}_{episode_id}")
    context = _build_context(item, index, task_base_dir=task_base_dir)

    run_pipeline(
        context,
        config,
        dry_run=args.dry_run,
        start_from=args.start_from,
        stop_after=args.stop_after,
        skip_existing=args.skip_existing,
        output_dir=output_dir,
        run_name=run_name,
        save_results=True,
    )

    trajectory = None
    trajectory_path = None
    refinement_record = _refinement_record(context)
    if refinement_record is not None:
        trajectory = build_trajectory(
            task=item,
            refinement_output=refinement_record.get("output"),
            video_meta=refinement_record.get("video_meta"),
        )
        trajectory_path = resolve_summary_output_path(
            item,
            summary_path,
            task_base_dir=task_base_dir,
        )

    return {
        "index": index,
        "total_tasks": total_tasks,
        "episode_id": episode_id,
        "run_name": run_name,
        "run_dir": context.get("run_dir"),
        "trajectory": trajectory,
        "trajectory_path": trajectory_path,
    }


def _handle_task_success(
    result: dict[str, Any],
    *,
    register_unique_output_path: Any,
    save_trajectory: Any,
    used_trajectory_paths: dict[str, str],
) -> None:
    trajectory = result.get("trajectory")
    trajectory_path = result.get("trajectory_path")
    if trajectory is not None and trajectory_path is not None:
        register_unique_output_path(trajectory_path, str(result["episode_id"]), used_trajectory_paths)
        save_trajectory(trajectory, trajectory_path)
        print(f"  trajectory -> {trajectory_path}")
    else:
        print("  trajectory skipped -> refinement stage was not completed")
    print(f"  ok -> {result.get('run_dir')}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch run configured VLM pipeline on robot_mind2 tasks.")
    parser.add_argument("--config", default=str(PACKAGE_DIR / "config.yaml"), help="Path to config.yaml.")
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
    parser.add_argument(
        "--summary",
        default=str(SCRIPT_DIR / "trajectory.json"),
        help="Path for aggregate summary JSON.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Run video/prompt pipeline without real model calls.")
    parser.add_argument("--start-index", type=int, default=0, help="Start task index, inclusive.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of tasks to process.")
    parser.add_argument("--start-from", default=None, help="Optional pipeline start stage.")
    parser.add_argument("--stop-after", default=None, help="Optional pipeline stop stage.")
    parser.add_argument("--skip-existing", action="store_true", help="Skip stages already present in context.")
    parser.add_argument("--workers", type=int, default=1, help="Number of episode pipelines to run concurrently.")
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop on first failed task instead of writing an error record and continuing.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.start_index < 0:
        raise ValueError("--start-index must be >= 0")
    if args.limit is not None and args.limit < 0:
        raise ValueError("--limit must be >= 0")
    if args.workers < 1:
        raise ValueError("--workers must be >= 1")

    (
        run_pipeline,
        build_trajectory,
        resolve_summary_output_path,
        save_trajectory,
        register_unique_output_path,
    ) = _ensure_imports()

    config_path = Path(args.config)
    tasks_path = Path(args.tasks)
    output_dir = Path(args.output_dir)
    summary_path = Path(args.summary)

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    tasks = _load_json_list(tasks_path)
    task_base_dir = tasks_path.resolve().parent
    selected_tasks = _slice_tasks(tasks, start_index=args.start_index, limit=args.limit)

    output_dir.mkdir(parents=True, exist_ok=True)
    used_trajectory_paths: dict[str, str] = {}
    ok_count = 0
    error_count = 0

    print(f"Loaded config: {config_path}")
    print(f"Loaded tasks: {tasks_path} ({len(tasks)} total, {len(selected_tasks)} selected)")
    print(f"Saving per-task outputs to: {output_dir}")
    print(f"Default trajectory output: {summary_path}")
    print(f"Workers: {args.workers}")

    def run_selected_task(task_pair: tuple[int, dict[str, Any]]) -> dict[str, Any]:
        index, item = task_pair
        return _run_one_task(
            item=item,
            index=index,
            total_tasks=len(tasks),
            task_base_dir=task_base_dir,
            config=config,
            output_dir=output_dir,
            summary_path=summary_path,
            args=args,
            run_pipeline=run_pipeline,
            build_trajectory=build_trajectory,
            resolve_summary_output_path=resolve_summary_output_path,
        )

    if args.workers == 1:
        for task_pair in selected_tasks:
            index, item = task_pair
            preview_name = _safe_name(
                f"{index:05d}_{_infer_dataset_name(item.get('video_path'))}_{item.get('episode_id') or f'item_{index:05d}'}"
            )
            print(f"\n[{index + 1}/{len(tasks)}] {preview_name}")
            try:
                result = run_selected_task(task_pair)
                _handle_task_success(
                    result,
                    register_unique_output_path=register_unique_output_path,
                    save_trajectory=save_trajectory,
                    used_trajectory_paths=used_trajectory_paths,
                )
                ok_count += 1
            except Exception as exc:
                error_text = f"{type(exc).__name__}: {exc}"
                error_count += 1
                print(f"  error -> {error_text}")
                if args.fail_fast:
                    traceback.print_exc()
                    raise
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            future_to_task = {executor.submit(run_selected_task, task_pair): task_pair for task_pair in selected_tasks}
            for future in as_completed(future_to_task):
                index, item = future_to_task[future]
                preview_name = _safe_name(
                    f"{index:05d}_{_infer_dataset_name(item.get('video_path'))}_{item.get('episode_id') or f'item_{index:05d}'}"
                )
                print(f"\n[{index + 1}/{len(tasks)}] {preview_name}")
                try:
                    result = future.result()
                    _handle_task_success(
                        result,
                        register_unique_output_path=register_unique_output_path,
                        save_trajectory=save_trajectory,
                        used_trajectory_paths=used_trajectory_paths,
                    )
                    ok_count += 1
                except Exception as exc:
                    error_text = f"{type(exc).__name__}: {exc}"
                    error_count += 1
                    print(f"  error -> {error_text}")
                    if args.fail_fast:
                        for pending in future_to_task:
                            pending.cancel()
                        traceback.print_exc()
                        raise
    print(f"\nDone. ok={ok_count}, error={error_count}, default_trajectory={summary_path}")


if __name__ == "__main__":
    main()
