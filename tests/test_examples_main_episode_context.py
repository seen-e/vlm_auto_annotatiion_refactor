from __future__ import annotations

import importlib
import sys
from pathlib import Path


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
