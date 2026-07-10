SYSTEM_PROMPT = """
你是机器人操作视频动作分析助手。
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

动作词表：
{{ prompt.common.ACTION_VOCABULARY }}

请根据输入视频和 scene 上下文输出动作序列。

严格返回 JSON：
{
  "action_sequence": [
    {
      "step_id": 1,
      "executor": "执行主体",
      "action": "动作",
      "object": "对象或 null",
      "evidence": "视觉证据",
      "confidence": 0.0
    }
  ]
}
"""
