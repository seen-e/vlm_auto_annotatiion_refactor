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
| `max_time` | float/int | `-1` 或 `>0` | 当前 stage 最大原始视频处理时长，单位秒；`-1` 表示不限制，`0` 或 `<-1` 非法 |
| `fps` | float | `>0` | 按原始时间轴采样 FPS |
| `max_frames` | int | `>0` | 最大采样帧/时间点数 |
| `resize_width` | int | `>0` | 单视角图像缩放宽度，保持比例 |
| `jpeg_quality` | int | `1-100` | JPEG 质量，越低越省 token/带宽 |
| `save_processed` | bool | true/false | 是否保存当前 stage 最终送入模型的处理后图像；保存到 `run_dir/stages/<stage_name>/processed_frames` |
| `draw_timestamps` | bool | true/false | 是否绘制时间戳 |
| `draw_view_names` | bool | true/false | 是否绘制视角名称 |
| `draw_montage_axes` | bool | true/false | 多视角 timeline montage 外侧是否绘制顶部时间戳和左侧视角标签 |
| `min_api_frames` | int | `>=1` | 尽量保证的最小输入帧数 |
| `merge_views` | bool | true/false | true 时不同视角按行纵向拼接；false 时只输出主视角 |
| `merge_mode` | str | `per_frame` / `timeline_grid` | 时间维度输出模式 |
| `merge_length` | int | 任意 int | timeline_grid 分组长度；小于 1 表示全部时间点合成一张图 |
| `view_names` | list[str] | 视角名列表 | 多视角选择与顺序，第一个通常为 primary view |

### `max_time` 处理语义

`max_time` 是每个 stage 独立配置的视频时长上限：

```yaml
scene:
  video:
    max_time: -1

analysis:
  video:
    max_time: 30

refinement:
  video:
    max_time: 60
```

规则：

1. `max_time: -1` 表示不限制，使用完整原始视频。
2. `max_time > 0` 的单位是秒，只处理原始视频时间轴上的 `[0, min(max_time, original_duration))`。
3. `max_time: 0` 或小于 `-1` 会抛出明确配置异常，不会静默回退。
4. 截断发生在抽帧、缩放、多视角合并、图像序列生成或编码之前。
5. `max_time` 不改变 `fps`、`max_frames`、`frame_start`、`frame_end`、`resize_width`、多视角合并等配置语义；如果同时存在其他时间或帧范围限制，会取所有限制的交集。
6. 多视角输入时，每个视角使用相同的上限：`min(该视角原始时长, max_time)`；较短视角不会被循环、补帧、减速或改时间戳。
7. 输出帧时间戳保留其相对于原始视频起点的真实时间戳。

`video_meta.json` 会记录实际处理范围，例如：

```json
{
  "original_duration": 120.0,
  "configured_max_time": 30.0,
  "effective_duration": 30.0,
  "was_time_limited": true
}
```

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
