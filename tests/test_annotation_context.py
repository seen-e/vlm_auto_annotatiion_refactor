from __future__ import annotations

import json

from examples.main import _build_context
from examples.main import _extract_scene_annotation
from examples.main import _save_batch_status
from examples.main import _selected_output_json_paths


def test_annotation_context_passes_trimmed_subtasks_with_two_decimal_times(tmp_path) -> None:
    video_path = tmp_path / "episode.mp4"
    item = {
        "episode_id": "episode_000001",
        "video_path": str(video_path),
        "task": "Move the cup.",
        "scenes": [{"scene_annotation": "A cup is on the table."}],
        "subtasks": [
            {
                "subtask_id": 7,
                "subtask_index": 99,
                "subtask": "Pick up the cup",
                "start_time": 0,
                "end_time": 5.2,
                "start_frame": 0,
                "end_frame": 156,
            }
        ],
    }

    context = _build_context(item, 0, task_base_dir=tmp_path)

    assert context["input"]["task"] == "Move the cup."
    assert context["input"]["scene_annotation"] == "A cup is on the table."
    assert '"start_time": 0.00' in context["input"]["subtasks"]
    assert '"end_time": 5.20' in context["input"]["subtasks"]
    assert "subtask_index" not in context["input"]["subtasks"]
    assert "start_frame" not in context["input"]["subtasks"]

    parsed = json.loads(context["input"]["subtasks"])
    assert parsed == [
        {
            "subtask_id": 7,
            "subtask": "Pick up the cup",
            "start_time": 0.0,
            "end_time": 5.2,
        }
    ]


def test_extract_scene_annotation_uses_first_scene_only() -> None:
    assert (
        _extract_scene_annotation(
            [
                {"scene_id": 0, "scene_annotation": "The cup is on the table."},
                {"scene_id": 1, "scene_annotation": "Ignored."},
            ]
        )
        == "The cup is on the table."
    )
    assert _extract_scene_annotation([]) == ""


def test_batch_status_files_and_output_json_verification(tmp_path) -> None:
    run_dir = tmp_path / "runs" / "episode_000001"
    output_json = run_dir / "stages" / "detail_annotation" / "output.json"
    output_json.parent.mkdir(parents=True)
    output_json.write_text("{}", encoding="utf-8")

    outputs = _selected_output_json_paths(
        {
            "run_dir": str(run_dir),
            "pipeline": {"selected_stages": ["detail_annotation"]},
        }
    )
    assert outputs == [{"stage": "detail_annotation", "output_json": str(output_json)}]

    _save_batch_status(
        tmp_path,
        [{"episode_id": "episode_000001", "output_json_paths": outputs}],
        [{"episode_id": "episode_000002", "error": "RuntimeError: failed"}],
    )

    assert "episode_000001" in (tmp_path / "successful_episodes.json").read_text(encoding="utf-8")
    assert "RuntimeError: failed" in (tmp_path / "failed_episodes.json").read_text(encoding="utf-8")
