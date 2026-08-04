from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any


PACKAGE_DIR = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PACKAGE_DIR.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

examples_main = importlib.import_module(f"{PACKAGE_DIR.name}.examples.main")


def test_build_context_preserves_episode_source_fields(tmp_path: Path) -> None:
    item = {
        "episode_id": "episode_000001",
        "task": "pick",
        "fps": 5.0,
        "length": None,
        "video_path": {
            "camera_front": "videos/front.mp4",
        },
        "video_segments": {
            "camera_front": {
                "video_path": "videos/big_front.mp4",
                "start_time": 1.0,
                "end_time": 2.0,
            }
        },
    }

    context = examples_main._build_context(item, 3, task_base_dir=tmp_path)

    episode = context["input"]["episode"]
    assert episode["fps"] == 5.0
    assert episode["length"] is None
    assert episode["video_path"]["camera_front"] == str((tmp_path / "videos" / "front.mp4").resolve())
    assert episode["video_segments"]["camera_front"]["video_path"] == str(
        (tmp_path / "videos" / "big_front.mp4").resolve()
    )
    assert context["input"]["video_id"] == "episode_000001"


def _write_stage_output(run_dir: Path, stage_name: str, output: dict[str, Any] | None = None) -> None:
    stage_dir = run_dir / "stages" / stage_name
    stage_dir.mkdir(parents=True, exist_ok=True)
    (stage_dir / "output.json").write_text(json.dumps(output or {"ok": stage_name}), encoding="utf-8")


def test_run_one_task_skips_episode_when_all_stage_outputs_exist(tmp_path: Path, monkeypatch) -> None:
    item = {
        "episode_id": "episode_000001",
        "task": "pick",
        "video_path": str(tmp_path / "front.mp4"),
    }
    config = {
        "workflow": ["scene", "analysis"],
        "stages": {"scene": {}, "analysis": {}},
    }
    output_dir = tmp_path / "outputs"
    run_name = "00003_task_episode_000001"
    run_dir = output_dir / run_name
    _write_stage_output(run_dir, "scene")
    _write_stage_output(run_dir, "analysis")

    def fail_if_called(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("run_pipeline should not be called for a completed episode")

    monkeypatch.setattr(examples_main, "_ensure_imports", lambda: fail_if_called)

    result = examples_main._run_one_task_worker(
        item=item,
        index=3,
        total_tasks=10,
        task_base_dir=str(tmp_path),
        config=config,
        output_dir=str(output_dir),
        run_options={
            "dry_run": False,
            "start_from": None,
            "stop_after": None,
            "skip_existing": True,
        },
    )

    assert result["status"] == "skipped_complete"
    assert result["run_dir"] == str(run_dir)
    assert result["existing_stage_names"] == ["scene", "analysis"]
    assert result["executed_stage_names"] == []


def test_run_one_task_resumes_from_existing_stage_outputs(tmp_path: Path, monkeypatch) -> None:
    item = {
        "episode_id": "episode_000002",
        "task": "pick",
        "video_path": str(tmp_path / "front.mp4"),
    }
    config = {
        "workflow": ["scene", "analysis"],
        "stages": {"scene": {}, "analysis": {}},
    }
    output_dir = tmp_path / "outputs"
    run_name = "00004_task_episode_000002"
    run_dir = output_dir / run_name
    _write_stage_output(run_dir, "scene", {"loaded": True})
    calls: list[dict[str, Any]] = []

    def fake_run_pipeline(context: dict[str, Any], config: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append({"context": context, "kwargs": kwargs})
        assert context["stages"]["scene"]["output"] == {"loaded": True}
        assert kwargs["skip_existing"] is True
        context.setdefault("pipeline", {})["executed_stages"] = ["analysis"]
        context["run_dir"] = str(run_dir)
        context.setdefault("stages", {})["analysis"] = {"output": {"new": True}}
        return context

    monkeypatch.setattr(examples_main, "_ensure_imports", lambda: fake_run_pipeline)

    result = examples_main._run_one_task_worker(
        item=item,
        index=4,
        total_tasks=10,
        task_base_dir=str(tmp_path),
        config=config,
        output_dir=str(output_dir),
        run_options={
            "dry_run": False,
            "start_from": None,
            "stop_after": None,
            "skip_existing": True,
        },
    )

    assert len(calls) == 1
    assert result["status"] == "completed"
    assert result["existing_stage_names"] == ["scene"]
    assert result["executed_stage_names"] == ["analysis"]


def test_scan_selected_tasks_skips_complete_episodes_with_threads(tmp_path: Path) -> None:
    tasks = [
        (3, {"episode_id": "episode_000003", "task": "pick", "video_path": str(tmp_path / "a.mp4")}),
        (1, {"episode_id": "episode_000001", "task": "pick", "video_path": str(tmp_path / "b.mp4")}),
        (2, {"episode_id": "episode_000002", "task": "pick", "video_path": str(tmp_path / "c.mp4")}),
    ]
    config = {
        "workflow": ["scene", "analysis"],
        "stages": {"scene": {}, "analysis": {}},
    }
    output_dir = tmp_path / "outputs"

    complete_run_dir = output_dir / "00001_task_episode_000001"
    partial_run_dir = output_dir / "00002_task_episode_000002"
    _write_stage_output(complete_run_dir, "scene")
    _write_stage_output(complete_run_dir, "analysis")
    _write_stage_output(partial_run_dir, "scene")

    executable_tasks, skipped_complete = examples_main._scan_selected_tasks(
        tasks,
        config=config,
        output_dir=str(output_dir),
        run_options={
            "dry_run": False,
            "start_from": None,
            "stop_after": None,
            "skip_existing": True,
        },
        scan_workers=8,
    )

    assert [index for index, _ in executable_tasks] == [2, 3]
    assert [state["index"] for state in skipped_complete] == [1]
    assert skipped_complete[0]["existing_stage_names"] == ["scene", "analysis"]
