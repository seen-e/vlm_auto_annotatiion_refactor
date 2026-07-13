"""Batch prediction entrypoint for robot_mind2 task JSON.

Default usage from the repository root:

    python examples/main.py

For a quick pipeline check without calling the VLM:

    python examples/main.py --dry-run --limit 1

Per-task outputs are saved under ``examples/batch_predictions`` and the
aggregate summary is saved to ``examples/batch_predictions.json`` by default.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
import traceback
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


def _ensure_imports() -> tuple[Any, Any]:
    """Import pipeline helpers from the current package directory name."""
    if str(PACKAGE_PARENT) not in sys.path:
        sys.path.insert(0, str(PACKAGE_PARENT))

    pipeline_module = importlib.import_module(f"{PACKAGE_NAME}.pipeline")
    result_io_module = importlib.import_module(f"{PACKAGE_NAME}.result_io")
    return pipeline_module.run_pipeline, result_io_module.save_json


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


def _slice_tasks(tasks: list[dict[str, Any]], *, start_index: int, limit: int | None) -> list[tuple[int, dict[str, Any]]]:
    end_index = len(tasks) if limit is None else min(len(tasks), start_index + limit)
    return list(enumerate(tasks[start_index:end_index], start=start_index))


def _build_context(item: dict[str, Any], index: int) -> dict[str, Any]:
    if "video_path" not in item:
        raise ValueError(f"Task item #{index} missing 'video_path'")
    instruction = item.get("task") or item.get("instruction")
    if not instruction:
        raise ValueError(f"Task item #{index} missing 'task' or 'instruction'")

    episode_id = str(item.get("episode_id") or f"item_{index:05d}")
    return {
        "input": {
            "video_path": item["video_path"],
            "instruction": str(instruction),
            "video_id": episode_id,
            "task_index": index,
        },
        "stages": {},
    }


def _summary_record(
    *,
    item: dict[str, Any],
    index: int,
    context: dict[str, Any] | None,
    status: str,
    error: str | None = None,
) -> dict[str, Any]:
    pipeline = context.get("pipeline", {}) if context else {}
    workflow = pipeline.get("workflow", [])
    final_stage = workflow[-1] if workflow else None
    final_output = None
    if context and final_stage in context.get("stages", {}):
        final_output = context["stages"][final_stage].get("output")

    record: dict[str, Any] = {
        "index": index,
        "episode_id": item.get("episode_id"),
        "task": item.get("task") or item.get("instruction"),
        "status": status,
        "run_dir": context.get("run_dir") if context else None,
        "workflow": workflow,
        "selected_stages": pipeline.get("selected_stages", []),
        "executed_stages": pipeline.get("executed_stages", []),
        "skipped_stages": pipeline.get("skipped_stages", []),
        "final_stage": final_stage,
        "final_output": final_output,
    }
    if error:
        record["error"] = error
    return record


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Batch run configured VLM pipeline on robot_mind2 tasks.")
    parser.add_argument("--config", default=str(PACKAGE_DIR / "config.yaml"), help="Path to config.yaml.")
    parser.add_argument(
        "--tasks",
        default=str(SCRIPT_DIR / "robogene_twoArm_franka_arrange_tabletop_drink_display.json"),
        help="Path to task JSON list.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(SCRIPT_DIR / "qwen3.5b_batch_predictions"),
        help="Directory for per-task outputs.",
    )
    parser.add_argument(
        "--summary",
        default=str(SCRIPT_DIR / "batch_predictions.json"),
        help="Path for aggregate summary JSON.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Run video/prompt pipeline without real model calls.")
    parser.add_argument("--start-index", type=int, default=0, help="Start task index, inclusive.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of tasks to process.")
    parser.add_argument("--start-from", default=None, help="Optional pipeline start stage.")
    parser.add_argument("--stop-after", default=None, help="Optional pipeline stop stage.")
    parser.add_argument("--skip-existing", action="store_true", help="Skip stages already present in context.")
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

    run_pipeline, save_json = _ensure_imports()

    config_path = Path(args.config)
    tasks_path = Path(args.tasks)
    output_dir = Path(args.output_dir)
    summary_path = Path(args.summary)

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    tasks = _load_json_list(tasks_path)
    selected_tasks = _slice_tasks(tasks, start_index=args.start_index, limit=args.limit)

    output_dir.mkdir(parents=True, exist_ok=True)
    summary: list[dict[str, Any]] = []

    print(f"Loaded config: {config_path}")
    print(f"Loaded tasks: {tasks_path} ({len(tasks)} total, {len(selected_tasks)} selected)")
    print(f"Saving per-task outputs to: {output_dir}")
    print(f"Saving summary to: {summary_path}")

    for index, item in selected_tasks:
        context: dict[str, Any] | None = None
        dataset_name = _infer_dataset_name(item.get("video_path"))
        episode_id = str(item.get("episode_id") or f"item_{index:05d}")
        run_name = _safe_name(f"{index:05d}_{dataset_name}_{episode_id}")

        print(f"\n[{index + 1}/{len(tasks)}] {run_name}")
        try:
            context = _build_context(item, index)
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
            summary.append(_summary_record(item=item, index=index, context=context, status="ok"))
            print(f"  ok -> {context.get('run_dir')}")
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            summary.append(_summary_record(item=item, index=index, context=context, status="error", error=error_text))
            print(f"  error -> {error_text}")
            if args.fail_fast:
                traceback.print_exc()
                save_json(summary, summary_path)
                raise

        save_json(summary, summary_path)

    ok_count = sum(1 for item in summary if item.get("status") == "ok")
    error_count = len(summary) - ok_count
    print(f"\nDone. ok={ok_count}, error={error_count}, summary={summary_path}")


if __name__ == "__main__":
    main()
