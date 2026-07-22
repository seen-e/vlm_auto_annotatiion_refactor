from __future__ import annotations

import importlib
import sys

from prompt_utils import _prompt_package_from_module, render_template


def test_prompt_placeholder_prefers_current_prompt_package(tmp_path, monkeypatch) -> None:
    package_dir = tmp_path / "prompts_custom"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "actionbase.py").write_text('ACTION_VOCABULARY = "custom actions"\n', encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    sys.modules.pop("prompts_custom.actionbase", None)

    rendered = render_template(
        "{{ prompt.actionbase.ACTION_VOCABULARY }}",
        context={},
        prompt_package="prompts_custom",
    )

    assert rendered == "custom actions"


def test_prompt_package_is_derived_from_stage_prompt_module() -> None:
    assert _prompt_package_from_module("prompts_4stage.gripper_prompt") == "prompts_4stage"
    assert _prompt_package_from_module("prompts.scene_dual") == "prompts"
