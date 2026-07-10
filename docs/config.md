# config.yaml 参数说明

`config.yaml` 是轻量实验框架的主配置文件。它控制：

1. 运行哪些 stage；
2. 每个 stage 使用哪个 prompt；
3. 每个 stage 的视频处理参数；
4. 模型调用参数；
5. 如何插入自定义 stage。

## 顶层结构

```yaml
workflow:
  - scene
  - analysis
  - refinement

model:
  base_url: ...
  api_key: ...
  model: ...
  max_tokens: ...
  temperature: ...

stages:
  scene:
    prompt: ...
    video: ...
```

## workflow

`workflow` 是 stage 名称列表，决定执行顺序。每个名称必须存在于 `stages` 中。

示例：

```yaml
workflow:
  - scene
  - object_state
  - analysis
  - refinement
```

如果某个 stage 的 prompt 引用了 `{{ ctx.stages.scene.output }}`，则 `scene` 必须在该 stage 前执行，或者通过 `result_io.load_outputs_into_context` 预先加载。

## model

| 参数 | 类型 | 作用 | 常见值 |
|---|---:|---|---|
| `base_url` | str | OpenAI-compatible API 地址 | `http://127.0.0.1:8002/v1` |
| `api_key` | str | API key，本地 vLLM 常用 `EMPTY` | `EMPTY` |
| `model` | str | 模型 id，需与 `/v1/models` 返回一致 | `Qwen3-VL...` |
| `max_tokens` | int | 默认最大输出 token | `4096` |
| `temperature` | float | 采样温度，标注建议 `0.0` | `0.0` |
| `top_p` | float/null | nucleus sampling | `0.95` 或 null |
| `top_k` | int | 额外采样参数；0 表示不传 | `0` |
| `max_retries` | int | 请求失败重试次数 | `3` |

## stages.<stage_name>.prompt

```yaml
prompt:
  module: "vlm_auto_annotation_refactor.prompts.analysis"
  system: "SYSTEM_PROMPT"
  user: "USER_PROMPT_TEMPLATE"
```

| 参数 | 作用 |
|---|---|
| `module` | Python prompt 模块路径 |
| `system` | 模块中的 system prompt 变量名 |
| `user` | 模块中的 user prompt 变量名 |

prompt 内可使用：

```text
{{ ctx.input.instruction }}
{{ ctx.stages.scene.output.executors }}
{{ prompt.common.JSON_ONLY_RULE }}
```

## stages.<stage_name>.video

| 参数 | 类型 | 可填写内容 | 说明 |
|---|---:|---|---|
| `input_mode` | str | `image_sequence` / `video` | 当前推荐 `image_sequence` |
| `fps` | float | `>0` | 按原始时间轴采样 FPS |
| `max_frames` | int | `>0` | 最大采样帧/时间点数 |
| `resize_width` | int | `>0` | 单视角图像缩放宽度，保持比例 |
| `jpeg_quality` | int | `1-100` | JPEG 质量，越低越省 token/带宽 |
| `draw_timestamps` | bool | true/false | 是否绘制时间戳 |
| `draw_view_names` | bool | true/false | 是否绘制视角名称 |
| `min_api_frames` | int | `>=1` | 尽量保证的最小输入帧数 |
| `merge_views` | bool | true/false | 是否把多视角同一时间点拼成一张图 |
| `merge_mode` | str | `per_frame` / `timeline_grid` | 拼接模式 |
| `merge_length` | int | `>=0` | 时间帧 montage 长度，0/1 通常表示不做时间拼接 |
| `view_names` | list[str] | 视角名列表 | 多视角选择与顺序，第一个通常为 primary view |

## generation

可选。写在某个 stage 下，用于覆盖顶层 `model` 参数：

```yaml
analysis:
  generation:
    max_tokens: 4096
    temperature: 0.0
```

## 自定义 stage

新增 stage 的最小步骤：

1. 新建 `vlm_auto_annotation_refactor/prompts/object_state.py`；
2. 在 `stages` 下新增 `object_state`；
3. 在 `workflow` 中插入 `object_state`；
4. 下游 prompt 通过 `{{ ctx.stages.object_state.output }}` 引用它。
