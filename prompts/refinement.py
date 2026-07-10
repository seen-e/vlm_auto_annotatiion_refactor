SYSTEM_PROMPT = """
你是机器人操作视频动作边界精修助手。
{{ prompt.common.JSON_ONLY_RULE }}
"""

USER_PROMPT_TEMPLATE = """
任务指令：
{{ ctx.input.instruction }}

视频输入布局：
{{ ctx.current_video_layout }}

{{ prompt.common.VIDEO_LAYOUT_RULE }}

Scene 阶段输出：
{{ ctx.stages.scene.output }}

Analysis 阶段动作序列：
{{ ctx.stages.analysis.output.action_sequence }}

时间边界规则：
{{ prompt.common.TIME_BOUNDARY_RULE }}

请根据带时间戳的视频精修每个动作的开始和结束时间。

严格返回 JSON：
{
  "refined_segments": [
    {
      "step_id": 1,
      "executor": "执行主体",
      "action": "动作",
      "object": "对象或 null",
      "start_time": 0.0,
      "end_time": 1.0,
      "evidence": "视觉证据",
      "confidence": 0.0
    }
  ]
}
"""
