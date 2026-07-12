# video_process.py

## 文件职责

`video_process.py` 是完全独立的视频输入层，不依赖原始项目中的 `utils/video_utils.py` 或其他旧模块。它把单视频或多视角视频转换为 VLM 可接收的 OpenAI-compatible `image_url` parts，并返回视频采样、视角、拼接和编码元信息。

它不负责 prompt、模型调用、JSON 解析或 stage 逻辑。

## 主要函数

### `build_video_inputs(video_path, *, fps, max_frames, resize_width, jpeg_quality, max_time=-1, draw_timestamps=True, draw_view_names=True, min_api_frames=1, frame_start=0, frame_end=None, merge_views=False, merge_mode="per_frame", merge_length=0, view_names=None, input_mode="image_sequence", save_processed_path=None)`

将视频输入转换成模型输入。

#### 参数

| 参数 | 类型 | 说明 |
|---|---|---|
| `video_path` | str / Path / list / dict | 单视频路径、多视角路径列表或 `{view_name: path}` 字典 |
| `fps` | float | 目标采样 FPS，基于原始视频时间轴 |
| `max_frames` | int | 最大采样时间点数，时间帧 montage 前生效 |
| `resize_width` | int | 每个视角图像的缩放宽度，保持宽高比 |
| `jpeg_quality` | int | JPEG 编码质量，范围 1-100 |
| `max_time` | float/int | 当前 stage 最大原始视频处理时长，单位秒；`-1` 表示不限制，`>0` 表示只处理原始时间轴 `[0, max_time)`，`0` 或 `<-1` 会报错 |
| `draw_timestamps` | bool | 是否绘制时间戳，例如 `t=1.25s` |
| `draw_view_names` | bool | 是否绘制视角名称；对应旧参数 `draw_viewposition` |
| `draw_montage_axes` | bool | 多视角 timeline montage 外侧是否绘制顶部时间戳和左侧视角标签 |
| `min_api_frames` | int | 视频足够时尽量保证的最少采样时间点数 |
| `frame_start` | int | 主视角起始帧索引，包含该帧 |
| `frame_end` | int/null | 主视角结束帧索引，包含该帧；null 表示到末尾 |
| `merge_views` | bool | true 时同一时间点的多视角图像按行纵向拼接；false 时只输出主视角 |
| `merge_mode` | str | `per_frame` 逐时间点输出；`timeline_grid` 按时间从左到右合并输出 |
| `merge_length` | int | timeline_grid 分组长度；小于 1 表示全部时间点合成一张图 |
| `view_names` | list[str]/null | 视角名称和顺序；对 dict 输入可用于选择/排序视角；对 list 输入必须与路径数量一致 |
| `input_mode` | str | 当前实现支持 `image_sequence`；`video` 会给出明确错误 |
| `save_processed_path` | str/Path/null | 可选保存处理后图像和 `video_meta.json` 的目录 |

#### 返回值

```python
parts, video_meta = build_video_inputs(...)
```

- `parts`：可直接放入 OpenAI-compatible message content 的 `image_url` parts。
- `video_meta`：dict，包含采样 FPS、时间戳、视角名、拼接模式、输出数量等信息。

`video_meta` 中与 `max_time` 相关的字段包括：

```json
{
  "original_duration": 120.0,
  "configured_max_time": 30.0,
  "effective_duration": 30.0,
  "was_time_limited": true
}
```

当 `max_time=-1` 或原始视频不超过 `max_time` 时，`was_time_limited=false`，`effective_duration` 等于实际参与处理的完整原始时长。

## 内部函数

| 函数 | 作用 |
|---|---|
| `normalize_video_input(video_path, view_names=None)` | 将单视频/list/dict 输入统一成有序 `{view_name: Path}` |
| `read_video_info(path)` | 读取视频 FPS、帧数、尺寸和时长 |
| `_effective_time_window(...)` | 计算 `max_time`、多视角时长和 `frame_start/frame_end` 的交集，得到最终有效处理区间 |
| `compute_sample_timestamps(...)` | 基于原始时间轴计算采样帧和时间戳 |
| `resize_keep_aspect(frame, resize_width)` | 保持宽高比缩放图像 |
| `draw_overlay(...)` | 绘制时间戳和视角名称 |
| `merge_view_frames(frames)` | 纵向拼接同一时间点的多个视角 |
| `merge_temporal_frames(frames)` | 将多个时间点图像按时间从左到右拼接成 montage |
| `encode_frame_to_image_part(frame, jpeg_quality)` | 编码为 base64 JPEG `image_url` part |
| `save_processed_frames(frames, video_meta, save_processed_path)` | 保存最终送入模型前的图像和元信息 |

## 典型用法

```python
from vlm_auto_annotation_refactor.video_process import build_video_inputs

parts, meta = build_video_inputs(
    {"camera_front": "front.mp4", "camera_top": "top.mp4"},
    fps=1.0,
    max_frames=64,
    resize_width=336,
    jpeg_quality=85,
    max_time=30,
    draw_timestamps=True,
    draw_view_names=True,
    merge_views=True,
    view_names=["camera_front", "camera_top"],
)
```

## 注意事项

1. 当前 standalone 版本不依赖原始项目任何代码。
2. 当前重点支持 `input_mode="image_sequence"`。
3. 多视角输入以第一个视角作为 primary view 和采样时间轴。
4. 不同视角帧数或 FPS 不一致时，其他视角会按主视角时间戳映射到各自视频帧。
5. `max_time` 在抽帧、缩放、多视角合并、时间帧 montage 和编码之前生效；它截断的是原始视频时间轴上的有效处理区间，不会改播放速度、FPS 或时间戳。
6. 多视角输入时，每个视角的有效时长是 `min(该视角原始时长, max_time)`；不会循环、补帧、减速或修改时间戳来补足较短视角。
7. 如果同时配置 `max_time` 和 `frame_start/frame_end`，最终只处理所有限制条件的交集；输出帧时间戳仍是相对于原始视频起点的真实时间戳。
8. `jpeg_quality` 越低，传输体积越小，但视觉细节损失越大。
