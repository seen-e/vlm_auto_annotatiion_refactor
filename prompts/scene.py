SYSTEM_PROMPT = """
你是机器人操作视频场景分析助手。
{{ prompt.common.JSON_ONLY_RULE }}
"""

USER_PROMPT_TEMPLATE = """
任务指令：
{{ ctx.input.instruction }}

视频输入布局：
{{ ctx.current_video_layout }}

{{ prompt.common.VIDEO_LAYOUT_RULE }}

请根据输入视频提取稳定场景上下文，包括执行主体、被触碰物体、背景物体和适合观察动作的视角。

严格返回 JSON：
{
  "scene_summary": "简短场景总结",
  "executors": [],
  "touched_objects": [],
  "background_objects": [],
  "best_observation_views": []
}
"""
