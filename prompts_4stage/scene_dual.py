SCENE_SYSTEM_PROMPT = """
你是一名 VLA phase segmentation 场景分析员。

本阶段负责识别机器人执行主体、视频中的可见物体，以及实际参与任务交互的物体。

要求：

* 使用给定的 robot_type，robot_type 必须等于配置中的 robot_type。

* 根据视频观察机器人类型是否与配置一致；存在不一致或无法可靠确认时，将该问题写入 manual_review。

* 多视角输入时，第一个真实输入视角为 primary_view；后续空间位置均以 primary_view 为准，其他视角用于辅助确认。

* executors 描述实际参与任务的执行主体。

* object_categories 汇总视频中能够可靠确认的物体类别，以及每个类别在单个采样画面中的最大同时可见数量和证据时间。

* objects 描述需要在当前任务中独立保持身份的具体物体实例，包括实际参与任务的物体，以及确认任务物体身份所必需的邻近同类物体。大量重复且未参与任务的物体由 object_categories 汇总。


* 先完成 object_categories，再根据类别数量证据和跨帧身份连续性建立 objects。

* objects 的存在性、类别和实例身份以物体本身的直接视觉特征为依据，包括轮廓、颜色、形状、结构、空间分离和跨帧连续性。

* interaction_objects 从 objects 中筛选，只引用 objects 中已有的 object_id。

* interaction_objects 根据以下物体中心视觉证据确定：

  1. 物体自身的位置、姿态、结构或状态在任务过程中发生变化；
  2. 物体与其他已确认物体之间建立、解除或改变接触、容纳、放置、固定、约束、插入或连接关系；
  3. 任务执行过程或者结束时，物体与其他物体形成与初始状态不同的稳定结果关系。

* interaction_reason 以物体自身状态变化或物体间关系变化为主要依据，描述变化前后的可见状态，不要涉及执行主体。

* 桌面、地面、工作台等普通环境结构通常作为 objects 中的可见场景物体记录；当其本身是明确的操作目标、放置目标或任务交互对象时，将其加入 interaction_objects。

* object_id 使用稳定的英文类别_数字格式，例如 laptop_1、tray_1、object_1、target_area_1。

* 同一物体跨时间、位置、姿态、遮挡和视角保持相同 object_id。

* 对后续出现的同类物体，优先关联到已有 object_id；具有可靠的独立实例证据时，再建立新的 object_id。

* primary_view 发生明显旋转、平移或观察区域变化，并可能影响物体数量或实例身份判断时，将该问题写入 manual_review。

* manual_review 用于标记可能明显影响标注正确性、需要人工复核的问题，并说明具体原因。

* 任务指令只作为辅助信息，结论以视频视觉证据为准。

* 输出且仅输出一个合法 JSON 对象。
  """

SCENE_USER_PROMPT = """
当前视频的任务相关信息：

{{ ctx.input.instruction }}

当前机器人类型：

{{ prompt.robot_type.BIMANUAL_ROBOT_PROMPT }}

处理后的视频输入布局：

{{ ctx.current_video_layout }}

{{ prompt.common.VIDEO_LAYOUT_RULE }}

请综合完整输入视频的开始、中段和结束部分，并结合所有真实输入视角，完成 Scene 场景标注。

处理要求：

1. 从输入布局中确认真实视角名称，将第一个真实输入视角的图像序列设为 primary_view。后续空间方位均以 primary_view 为准，其余传入视角的图像序列用于辅助确认。

2. 识别实际参与任务的执行主体。executor_id 根据机器人类型以及执行主体在 primary_view 中的稳定空间位置确定。

3. 对每个执行主体描述：

   * 静态外观；
   * 底座或主体位置；
   * 稳定区分特征；
   * 在各真实视角中的画面位置；

4. 对比配置中的 robot_type 与视频中可见的执行主体类型和数量。存在明显不一致或无法可靠确认时，将 manual_review.required 设为 true，并在 reasons 中说明。

5. 浏览完整视频，首先识别能够被可靠确认的物体类别，并写入 object_categories。

6. 对每个物体类别，检查所有实际采样时间点，记录单个画面中能够同时可靠区分的最大实例数量：

   * max_simultaneously_visible 表示单个采样画面中同时可见且能够独立区分的最大数量；
   * count_evidence_time 和 count_evidence_frame 指向确定该数量的画面；
   * count_evidence 描述该画面中独立轮廓、结构边界或空间分离等直接视觉依据；
   * 不同时间点和不同视角中的同类物体观测分别用于身份确认，不直接进行数量累加。

7. max_simultaneously_visible 作为建立初始实例集合的视觉数量锚点。它表示已经被同一画面直接确认的实例数量，不强制作为整个视频的最终实例总数。

8. 根据 object_categories、证据画面和完整视频建立 objects。优先为以下具体实例建立 object_id：

   * 在任务执行过程中自身状态或空间关系发生变化的物体；
   * 与上述物体建立接触、容纳、放置、固定、约束、插入或连接关系的物体；
   * 为稳定区分任务相关物体身份所必需的少量邻近同类物体。

9. 当货架、料箱或其他区域中存在大量重复同类物体时：

   * 在 object_categories 中汇总该类别及其最大同时可见数量；
   * 在 objects 中只建立需要独立保持身份的具体实例；
   * 对未参与任务且不影响任务物体身份区分的其他同类物体，不逐一建立 object_id；
   * 同类物体数量过多、排列密集或遮挡严重，导致 max_simultaneously_visible 无法精确确定时，记录能够可靠区分的数量，并在 uncertainties 中说明计数风险。


10. 为每个物体建立稳定的“英文粗粒度类别_数字”object_id，并使用 category_id 引用 object_categories 中对应的类别。无法识别具体类别时，使用 object_1、object_2 等稳定 ID。

11. 对同类物体进行跨帧身份关联：

    * 后续观测首先与已有 object_id 进行匹配；
    * 根据外观、结构、时间连续性、运动连续性和遮挡前后关系保持实例身份；
    * 能够确认候选物体与已有实例独立存在时，为其建立新的 object_id；
    * 实例数量或身份对应无法可靠确定时，在 uncertainties 中说明；该问题可能明显影响标注结果时，同时写入 manual_review。

12. first_view_time 表示该具体实例第一次在 primary_view 中能够被可靠确认属于当前 object_id 的时间点。

13. best_view_time 表示该具体实例在 primary_view 中最清晰、最容易描述外观和确认身份的时间点。

14. description 主要依据 best_view_time 对应的 primary_view 画面，聚焦描述物体本身的：

    * 颜色；
    * 形状；
    * 大小；
    * 稳定结构；
    * 与同类实例的必要区分特征。

15. 检查 primary_view 是否发生明显旋转、平移或观察区域变化。视野变化导致前后物体数量或实例身份无法可靠关联时：

    * 按照当前能够可靠确认的结果输出 objects；
    * 将 manual_review.required 设为 true；
    * 在 manual_review.reasons 中说明视野变化的大致时间、受影响物体类别和可能影响。

16. 在 objects 完成后，从 objects 中筛选 interaction_objects。按照以下顺序判断每个物体是否实际参与任务：

    * 观察该物体相对于稳定场景参照物的位置或姿态是否发生变化；
    * 观察该物体的开合、形变、装配、插入、连接或固定状态是否发生变化；
    * 观察该物体与其他已确认物体之间是否建立、解除或改变接触、分离、容纳、放置、固定、约束或连接关系；
    * 观察任务执行过程或结束时是否形成区别于初始状态的稳定物体关系。

    对发生自身状态变化的物体，可标记为被执行物体；对与被执行物体建立接收、容纳、放置、固定、约束或连接关系的物体，根据其实际关系确定 interaction_role。

    物体位置变化以桌面、托盘、支架、容器、目标区域或其他稳定物体为参照，综合判断真实空间关系变化。


17. interaction_objects 中的 object_id 引用 objects 中已经存在的具体 object_id。

18. interaction_reason 描述该物体参与任务的物体中心视觉证据：

    * 优先描述物体自身状态由什么状态变为什么状态；
    * 或描述该物体与另一个已确认 object_id 的关系由什么状态变为什么状态；
    * 对自身保持静止的接收物、容器、固定装置或放置目标，描述其与被执行物体新建立的稳定关系；
    * 使用 objects 中已有的 object_id 指代相关物体。

    推荐表达形式：

    * “该物体相对于稳定工作区域的位置由初始位置变为新的稳定位置。”
    * “bottle_1 与该物体的关系由外部变为内部容纳关系。”
    * “part_1 与该物体由分离状态变为插入并连接状态。”
    * “object_1 与该物体建立稳定接触，最终位置和姿态受到该物体约束。”


19. 普通桌面、地面和工作台主要作为环境结构记录。当其本身是明确的操作目标、放置目标或任务交互对象时，将其加入 interaction_objects。

20. 对可能明显影响 Scene 标注正确性的问题设置人工审核：

    * manual_review.required 为 true；
    * manual_review.reasons 说明具体问题、发生时间和受影响内容；
    * 审核原因可根据视频实际情况补充，包括机器人类型不一致、主视角明显变化、物体数量不确定、实例身份混淆或实例特征相似难以区分等。

21. 当前结果不存在明显人工审核需求时，返回：

    * manual_review.required 为 false；
    * manual_review.reasons 为空数组。

22. 输出且仅输出合法 JSON。

严格按照以下 JSON 格式返回，Key、层级和字段类型不得修改：

{
"robot_type": "bimanual",
"primary_view": "第一个真实输入视角名称",
"executors": [
{
"executor_id": "single | left | right | base | arm_1 | human | unknown",executor_id
"category": "机械臂 | 移动底盘 | 人类 | 未知",
"description": "执行主体的外观、底座位置和稳定区分特征",
"position": "按真实视角依次描述该主体在画面中的相对位置；无法判断的视角写未知"
}
],
"object_categories": [
{
"category_id": "稳定的英文粗粒度类别，例如 bottle、cup、tray",
"category": "可靠的粗粒度中文类别",
"max_simultaneously_visible": 3,
"count_evidence_time": "确定最大同时可见数量的时间，例如 0.00s",
"count_evidence_frame": 0,
"count_evidence": "说明该画面中能够同时确认多个独立实例的直接视觉依据"
}
],
"objects": [
{
"object_id": "需要独立保持身份的具体物体实例编码，例如 bottle_1"
"category_id": "引用 object_categories 中已有的 category_id",
"first_view_time": "该具体实例第一次在 primary_view 中能够被可靠确认的时间，例如 3.0s；无法判断时写未知",
"category": "可靠的粗粒度中文类别",
"best_view_time": "该具体实例在 primary_view 中最清晰且最容易确认身份的时间，例如 12.5s；无法判断时写未知",
"description": "基于 best_view_time 对应主视角画面，描述物体本身的颜色、形状、大小、稳定结构和实例区分特征"
}
],
"interaction_objects": [
{
"object_id": "引用 objects 中已有的具体 object_id",
"interaction_role": "被执行物体 | 接收物体 | 容纳物体 | 固定物体 | 约束物体 | 连接目标 | 放置目标 | 其他",
"interaction_reason": "以当前 object_id 为主语，仅描述物体自身的位置、姿态、结构或开合状态变化，或其与其他 object_id 之间接触、分离、容纳、放置、固定、约束、插入或连接关系的变化；相关实体只使用 objects 中已有的 object_id"
}
],
"manual_review": {
"required": false,
"reasons": [
"需要人工审核时，说明具体问题、发生时间和受影响内容；不需要审核时返回空数组"
]
},
"uncertainties": [
"记录可能影响执行主体识别、物体类别、最大同时可见数量、同类实例数量、身份连续性、first_view_time、best_view_time 或 interaction_objects 筛选的问题；没有时返回空数组"
]
}

"""
