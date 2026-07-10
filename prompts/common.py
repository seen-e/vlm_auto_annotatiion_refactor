JSON_ONLY_RULE = """
严格只输出 JSON，不要输出 Markdown、解释、代码块或额外文本。
"""

ACTION_VOCABULARY = [
    "approach",
    "grasp",
    "lift",
    "move",
    "place",
    "release",
    "push",
    "pull",
    "rotate",
    "open",
    "close",
    "insert",
    "withdraw",
]

TIME_BOUNDARY_RULE = """
时间边界应基于画面中接触、抓取、释放、物体状态变化等可观察事件判断。
start_time 必须小于 end_time；无法判断时给出 null 并说明原因。
"""

VIDEO_LAYOUT_RULE = """
请先理解视频输入的拼图布局，再分析场景、动作或时间边界。
拼图中的相邻子图不一定表示真实空间相邻，可能表示不同视角或不同时间点。
判断动作顺序和时间边界时，应优先参考图中的时间戳 t=...s。
判断物体相对位置时，应结合视角名称，不要把不同视角之间的左右关系直接当成真实世界左右关系。
如果一个图像是时间网格或 montage，请按时间戳和从左到右、从上到下的网格顺序理解时间推进。
"""
