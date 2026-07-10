# Dataflow

本文档说明一次完整 pipeline 的数据流，以及 `context`、prompt 占位符、dry-run 和结果保存如何协作。

## 总体流程

```text
config.yaml
  ↓
pipeline.run_pipeline
  ↓
stage_runner.run_stage
  ↓
video_process.build_video_inputs
  ↓
prompt_utils 渲染 system/user prompt
  ↓
model_client.call_vlm
  ↓
json_utils.extract_json
  ↓
context["stages"][stage_name]
  ↓
result_io 保存结果
```

真实执行顺序：

```text
examples/run_pipeline.py
  -> 读取 config.yaml
  -> 构造 context["input"]
  -> pipeline.run_pipeline(context, config)
       -> 校验 workflow
       -> 裁剪 selected_stages
       -> 对每个 stage 调用 stage_runner.run_stage
            -> video_process.build_video_inputs
            -> prompt_utils.load_stage_prompt
            -> prompt_utils.render_template
            -> dry_run ?
                 yes: 使用 _dry_run_output
                 no : call_vlm -> extract_json
            -> 写入 context["stages"][stage_name]
       -> 可选 save_context
```

## 视频输入数据流

`stage_runner.py` 调用：

```python
image_parts, video_meta = build_video_inputs(context["input"]["video_path"], **video_cfg)
```

`video_process.py` 内部流程：

```text
video_path
  ↓
normalize_video_input
  ↓
read_video_info
  ↓
compute_sample_timestamps  # primary view 时间轴
  ↓
_build_timepoint_frames
  ↓
resize_keep_aspect
  ↓
draw_overlay
  ↓
merge_view_frames          # merge_views=True
  ↓
_apply_temporal_merge      # merge_length > 1
  ↓
encode_frame_to_image_part
  ↓
image_parts + video_meta
```

重要细节：

1. `video_path` 可是单路径、路径 list 或 `{view_name: path}` dict。
2. dict 输入时，`view_names` 可选择和排序视角。
3. 第一个视角作为 primary view，用于抽帧和时间轴。
4. 其他视角按 primary timestamp 映射到自己的帧。
5. 当前只实现 `input_mode="image_sequence"`；`input_mode="video"` 会报错。

## `context` 结构

最小输入：

```python
context = {
    "input": {
        "video_path": "/path/to/video.mp4",
        "instruction": "pick up the cup",
        "video_id": "demo_episode"
    },
    "stages": {}
}
```

`context["input"]` 当前常见字段：

| 字段 | 作用 |
|---|---|
| `video_path` | 传给 `video_process.build_video_inputs()`；必需。 |
| `instruction` | 被默认 prompts 通过 `{{ ctx.input.instruction }}` 引用。 |
| `video_id` | 保存结果时可作为默认 run name。 |

`pipeline.run_pipeline()` 会补充：

```python
context["pipeline"] = {
    "workflow": [...],
    "selected_stages": [...],
    "executed_stages": [...],
    "skipped_stages": [...]
}
```

如果 `save_results=True`，还会写入：

```python
context["run_dir"] = "outputs/<run_name>"
```

## `context["stages"][stage_name]`

每个 stage 运行后写入：

```python
context["stages"][stage_name] = {
    "output": parsed_json,
    "raw_text": raw_text,
    "system_prompt": system_prompt,
    "prompt": user_prompt,
    "video_meta": video_meta,
    "usage": usage,
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `output` | 模型输出解析后的 JSON，或 dry-run 假输出。 |
| `raw_text` | 模型原文，或 dry-run JSON 字符串。 |
| `system_prompt` | 渲染后的 system prompt。 |
| `prompt` | 渲染后的 user prompt。 |
| `video_meta` | 抽帧、视角、拼接、编码等 metadata。 |
| `usage` | 当前为空 dict，因为 `stage_runner.py` 调用的是 `call_vlm()`。 |

## 上游输出如何传给下游 stage

上游输出存在：

```python
context["stages"][stage_name]["output"]
```

下游 prompt 通过占位符引用：

```text
{{ ctx.stages.scene.output }}
{{ ctx.stages.scene.output.executors }}
{{ ctx.stages.analysis.output.action_sequence }}
```

当前默认 prompt 依赖关系：

```text
scene
  ↓
analysis: 引用 ctx.stages.scene.output
  ↓
refinement: 引用 ctx.stages.scene.output 和 ctx.stages.analysis.output.action_sequence
```

## `{{ ctx.stages.scene.output.xxx }}` 如何解析

`prompt_utils.render_template()` 发现 `ctx.` 前缀后调用：

```python
resolve_context_path(context, path)
```

解析规则：

1. dict：路径片段作为 key。
2. list/tuple：路径片段必须是整数下标。
3. 普通对象：尝试属性访问。
4. 任一路径不存在会抛 `PromptRenderError`。

示例：

```text
{{ ctx.stages.analysis.output.action_sequence.0.action }}
```

会依次访问：

```text
stages -> analysis -> output -> action_sequence -> 0 -> action
```

## `{{ prompt.common.xxx }}` 如何解析

`prompt.` 前缀会触发：

```python
resolve_prompt_path("common.JSON_ONLY_RULE")
```

解析过程：

1. 动态导入 `prompts.common` / `vlm_auto_annotation_refactor.prompts.common`。
2. 读取变量。
3. 用 `_to_text()` 转成 prompt 文本。

常见引用：

```python
{{ prompt.common.JSON_ONLY_RULE }}
{{ prompt.common.ACTION_VOCABULARY }}
{{ prompt.common.TIME_BOUNDARY_RULE }}
```

list/dict 会被转成 pretty JSON 字符串。

## `dry_run=True`

仍会执行：

1. context 检查。
2. stage 配置读取。
3. 视频处理：`build_video_inputs()`。
4. prompt 加载和渲染。
5. context 写入。
6. 可选结果保存。

会跳过：

1. `model_client.call_vlm()`。
2. `json_utils.extract_json()`。

替代输出来自 `_dry_run_output(stage_name)`：

| stage | dry-run output |
|---|---|
| `scene` | `scene_summary`、`executors`、`touched_objects`、`background_objects`、`best_observation_views` |
| `analysis` | `action_sequence` |
| `refinement` | `refined_segments` |
| 其他 stage | `{"stage": stage_name, "status": "dry_run_ok"}` |

注意：dry-run 不需要真实模型服务，但仍需要视频文件存在，并且需要 `opencv-python` / `numpy` 可用。

## `skip_existing=True`

`pipeline.run_pipeline()` 对每个 selected stage 检查：

```python
if skip_existing and stage_name in context.get("stages", {}):
    skipped.append(stage_name)
    continue
```

用途：先用 `result_io.load_outputs_into_context()` 加载上游结果，然后跳过已有 stage，只跑下游。

示例：

```python
load_outputs_into_context(context, "outputs/demo", ["scene", "analysis"])
run_pipeline(context, config, start_from="scene", stop_after="refinement", skip_existing=True)
```

如果 selected stages 只有下游，例如 `start_from="refinement"`，则不一定需要 `skip_existing=True`。

## 保存结果

`save_results=True` 时：

```text
pipeline.run_pipeline
  -> result_io.make_run_dir
  -> stage_runner.run_stage(..., save_result=True)
       -> result_io.save_stage_result
  -> result_io.save_context
```

目录结构：

```text
outputs/<run_name>/
  input.json
  context.json
  stages/<stage_name>/
    system_prompt.txt
    user_prompt.txt
    raw_text.txt
    output.json
    video_meta.json
    usage.json
```
