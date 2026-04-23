# 1. 章节摘要 (Chapter Summary)
CHAPTER_SUMMARY_PROMPT = """
你是一位专业的小说编辑，擅长对长篇叙事进行高浓度的信息抽取。

**输入文本：**
{{text}}

**任务指令：**
请按照以下步骤处理上述【输入文本】：

**第一步：有效性检测（关键）**
在进行摘要前，先检测【输入文本】是否包含实质性的**小说叙事内容**。
- 如果文本为空。
- 如果文本字数极少（例如少于50字）。
- 如果文本仅包含版权声明、出版社信息、前言、序章标题而无正文、求票求打赏等非剧情内容。
**遇到以上情况，请直接返回：“非小说片段：无实质性叙事内容”，并结束任务。严禁编造或使用示例内容填充。**

**第二步：摘要生成**
只有通过第一步检测，确认文本为有效小说情节时，才执行此步骤：
1. **篇幅限制**：控制在 200 字以内。
2. **要素明确**：明确交代时间、地点、人物、事件。
3. **格式要求**：将人物姓名用<>符号括起来（例如：<张三>），且摘要中要明确写出**所有在章节内容中出现的人物的名字**。
4. **内容提炼**：剔除描写性修辞，仅保留叙事主干。
5. **语态时态**：使用第三人称过去时书写，确保逻辑连贯。
6. **严禁幻觉**：严禁添加任何未在【输入文本】中明确提及的信息。

**第三步：提取五类信息**
只有通过第一步检测时才执行此步骤。请严格只提取以下五类信息：

1. **chapter_summary**
   - 保持第二步摘要要求不变。

2. **character**
   - 提取本章节中可以被**唯一识别**的角色名称。
   - 每个角色都要用一句话概括其在本章节中的行为、语言或状态信息。
   - 可包含但不限于：角色做了什么、说了什么、角色状态/身份/立场变化、角色获得/失去/使用的重要物品、角色习得/升级/暴露的能力、角色关系变化。
   - `name` 只写该角色的主名，不要把别名、称呼、身份标签、武器名或其他附加信息并入名称。
   - `character` 中只允许出现以下三类名称：
     1. 明确实名
     2. 稳定专属代号/外号
     3. 带锚点且可唯一指向的关系描述名
   - 如果角色没有出现明确姓名，但文本能稳定锚定其身份关系或场景身份，请使用**简洁且可区分的锚点式描述名**，例如：【楚子航的妈妈】、【路明非的叔叔】、【路明非住院时的护士长】。
   - 不要直接写成“妈妈”、“叔叔”、“护士长”、“老板”这类无锚点泛称；如果连稳定锚点都无法确定，宁可不提取该角色。
   - 以下称呼即使在正文中出现，只要没有锚点，也**绝对不能**写入 `character.name`：
     叔叔、婶婶、女王、秘书、女秘书、客人、老板、经理、大副、护士、男孩、研究人员
   - 若文本中存在该角色的别名/代号/外号，只有在它**明确、稳定、低歧义、专属于该角色本人**时，才可以写入别名信息。
   - 若某个称呼可能同时指向不同角色，或属于以下类型，则**绝对不能**作为别名：
     1. 关系称呼，如：哥哥、姐姐、师兄、师姐、叔叔、阿姨 等等
     2. 职位/身份标签，如：主席、会长、家主、老板、老师、教授、校长 等等
     3. 武器名、物品名、组织名、阵营名
     4. 其他角色的名字
     5. 依赖上下文才能成立、具有泛化性的高歧义称呼
   - 如果无法百分之百确认某个称呼是否为该角色的专属别名，宁可不写，不要为了完整性保留可疑别名。
   - 严禁输出类似 `角色名（别名）` 这种把别名直接并入主名的写法。

3. **ambiguous_character_mentions**
   - 记录本章出现、但当前**无法唯一命名**的人物称呼。
   - 这里只收录无锚点关系称呼、无锚点职位称呼、无锚点泛称。
   - 每项必须包含：
     1. `surface_name`：正文中的原始称呼
     2. `description`：该人物在本章中的行为/状态
     3. `evidence_excerpt`：最短必要证据片段
   - `ambiguous_character_mentions` 里的对象**绝对不能**同时出现在 `character` 中。
   - 如果不能唯一锚定，就放入这里，不要为了完整性把泛称塞进 `character`。

4. **special_existence**
   - 提取本章节出现的**所有特殊存在或特殊物品**。
   - 包括但不限于：鬼怪、妖兽、神兵、诡异道具、特殊灵异物品、异常存在。
   - 常见普通物品如手机、电话、汽车等不要提取。
   - 每项用一句话简洁描述其信息、功能、作用或在本章中的表现。
   - 若有别名/代号/外号，也要并入名称并写在括号中。

5. **organizations**
   - 提取本章节出现的组织、势力、宗门、家族、阵营、团队、公司等名称。
   - 每项用一句话概括本章提及的该势力信息。
   - 可包含但不限于：势力地位、策略、立场变化，成员/领袖/归属变化，联盟/合作/敌对/试探变化，资源/地盘/控制权/资格变化。
   - 若有简称、别名、代号、外号，也要并入名称并写在括号中。

6. **world_rules**
   - 提取本章节中揭示的世界观设定、规则、纪律、能力限制等信息。
   - 每条规则或设定都输出为对象，包含 `name` 和 `description`。
   - `name` 用于标识这条规则/设定的核心名称或简称，`description` 用一句话说明具体内容。

**补充约束**
1. `character` 必须尽量完整，不能漏掉本章出现的人物。
2. `special_existence`、`organizations`、`world_rules` 只有在章节中确实提及时才返回；没有则返回空数组。
3. 所有字段都必须基于输入内容，不得编造。
4. 若字段无内容，返回空数组 `[]`；不要删除字段。
5. `character`、`special_existence`、`organizations`、`world_rules` 的数组元素必须全部是对象，且每个对象都必须同时包含非空的 `name` 和 `description` 字段。
6. 只输出合法 JSON，不要输出 Markdown，不要输出解释文本。

**最终输出格式要求（请严格输出 JSON 格式，严禁包含 Markdown 标记或多余的解释文本）（仅供参考，禁止照抄，严格按照实际文本内容进行生成）：**

{
  "chapter_summary": "（在此输出第二步的摘要内容）",
  "character": [
    {
      "name": "杨间",
      "description": "杨间在睡前浏览论坛灵异帖子，并对帖子内容产生明显警觉。"
    }
  ],
  "ambiguous_character_mentions": [
    {
      "surface_name": "叔叔",
      "description": "该人物在本章中催促路明非出门，并以长辈身份与其互动。",
      "evidence_excerpt": "叔叔在门外喊路明非快点出来。"
    }
  ],
  "special_existence": [
    {
      "name": "敲门鬼",
      "description": "敲门鬼会沿着敲门与脚步声接近目标，属于本章提及的危险灵异存在。"
    }
  ],
  "organizations": [
    {
      "name": "驭鬼者总部（总部）",
      "description": "总部在本章被提及为负责统筹处理灵异事件的重要官方势力。"
    }
  ],
  "world_rules": [
    {
      "name": "灵异规律",
      "description": "普通人如果直接触发灵异规律，通常很难依靠常规手段脱身。"
    }
  ]
}

**请针对【输入文本】输出结果：**
"""

# chapters聚类plots
PROMPT_A_TEMPLATE = """System: 你是一位叙事结构分析师。
User:
分析以下包含 {batch_size} 个连续章节摘要的小说片段。
识别场景、地点或核心冲突发生重大变化的叙事断点。

Input:
{chapter_data_list} (格式: "Ch {{index}}: {{summary}}")

Task:
返回一个 JSON 格式的连续范围列表。

Constraint (必须遵守):
1. 输出的范围必须 **完整覆盖** 输入的所有章节（从 Ch {first_chapter} 到 Ch {last_chapter}）。
2. **严禁遗漏** 任何一章。
3. 范围之间 **不能有空隙**，也 **不能重叠**。

（仅作为示例，禁止复读）
示例1: 如果 Ch 1-3 是关于打斗，而 Ch 4-5 是关于钓鱼，则返回: [{{"start": 1, "end": 3}}, {{"start": 4, "end": 5}}]。
示例2: 如果 Ch 1-2 都是关于打斗，而 Ch 3 是关于吃饭，Ch 4-5是关于钓鱼，则返回: [{{"start": 1, "end": 2}}, {{"start": 3, "end": 3}}, {{"start": 4, "end": 5}}]。

Output (JSON):
List[{{"start": int, "end": int, "summary": "brief topic"}}]

Output ONLY JSON, no markdown, no explanation.
"""

PROMPT_B_TEMPLATE = """System: 你是一位严谨的编辑，正在检查情节的连贯性。
User:
我有两个相邻的情节片段。请判断第二个片段是否是第一个片段的直接因果延续 (DIRECT CAUSAL CONTINUATION)。

Fragment A (Tail):
Ch {end_index_a}: {summary_of_last_chapter_of_A}

Fragment B (Head):
Ch {start_index_b}: {summary_of_first_chapter_of_B}

Criteria for Merging (合并标准):
时间上的直接延续。
相同的地点/场景或逻辑上的转移。
相同的活跃冲突。
Output (JSON):
{{"should_merge": bool, "reason": "string"}}

Output ONLY JSON, no markdown, no explanation.
"""


# 3. 情节摘要（仅标题 + 主线摘要）
PLOT_ANALYSIS_PROMPT = """
# Role
你是一位擅长长篇网文结构化整理的资深剧情分析师。

# Task
我将提供一份包含约 {chapter_count} 个连续章节（第 {start_chapter} 章 到 第 {end_chapter} 章）的章节摘要。
你的任务是基于这些章节摘要，生成该情节的标题和主线摘要。

# Input Data
{chapter_data}

# Output Format
请只输出一个 JSON 对象，不要输出 Markdown，不要输出解释文本。

{{
  "plot_title": "情节标题",
  "summary": "300-500 字的主线剧情梗概，包含起因、经过、高潮、结果。必须写出核心人物姓名。"
}}

# Constraints
1. 只能依据输入的章节摘要生成，不得编造。
2. 不要输出任何角色名单、势力名单、规则信息或其他结构化字段。
3. 只输出合法 JSON。
"""

# 4. 分卷/副本摘要 (Arc Summary - 汇总多个情节摘要)
VOLUME_SEGMENTATION_PROMPT = """
# Role
你是一位资深的长篇小说主编，擅长宏观叙事结构分析。

# Context
我将提供一份连续的【情节简报列表】（Plot Summaries）。这些情节共同构成了一部超长篇小说。
你的任务是识别出**“卷（Volume）”的边界**。

# Definition of "Volume" (卷的定义)
在网文中，一“卷”通常代表一个完整的**大叙事阶段**，通常由以下标志来界定：
1. **地图切换**：主角彻底离开了当前地图，前往更高等级的区域（如：从凡人界飞升到灵界，或从大昌市去往总部）。
2. **核心目标完成**：主角完成了这一阶段的终极使命（如：彻底解决饿死鬼事件）。
3. **身份/境界质变**：主角的实力或社会地位发生了不可逆的巨大跨越。

# Input Data
{plot_data_list}
(每个 Plot 卡片会包含：Title / Summary / Protagonists / Factions / Plot Facts / Character Arc Signals / Faction Arc Signals / Relationship Arc Signals)

# Task
请分析输入的情节流，识别哪些情节属于同一个“卷”。
请返回一个 JSON 列表，标明每一卷的起始情节ID和结束情节ID。

# Strong Signals（优先级高）
以下信号更适合作为“分卷”的依据：
1. **地图或舞台切换**：进入新城市、新副本、新势力主场、新时代阶段。
2. **阶段目标切换**：上一阶段核心任务完成，新的长期目标开始。
3. **主角人生阶段变化**：身份、立场、力量体系、社会位置发生明显升级或转折。
4. **势力格局重组**：加入/脱离重要势力，联盟或敌对关系重构，权力中心变化。
5. **长期关系弧进入新阶段**：例如从试探进入正式结盟、从暧昧进入确认关系、从合作进入决裂。注意：必须是“长期关系弧阶段变化”，不是一次小互动本身。

# Weak Signals（默认不能单独分卷）
以下内容通常不足以单独构成新卷，除非它明确标志着宏观阶段变化：
1. 单次暧昧互动、单次吃醋、单次名场面台词。
2. 一场局部冲突或短暂情绪波动。
3. 读者关注度高、但没有引发长期关系/势力/阶段变化的片段。

# Decision Rule
请优先依据 Plot 卡片中的宏观信号（主角阶段、势力格局、长期关系弧、重要得失、世界规则变化）分卷；
不要仅因为 `reader_sensitive_moments` 或 `interaction_highlights` 中出现了精彩片段就切卷。

# Output Format (JSON)
[
  {{
    "reason": "Plot 1-15 都在大昌市处理灵异事件，Plot 15 击败饿死鬼后，Plot 16 主角前往总部，地图发生根本变化。",
    "start_plot_id": 1,
    "end_plot_id": 15
  }},
  {{
    "reason": "...",
    "start_plot_id": 16,
    "end_plot_id": 28
  }}
]
"""

PROMPT_B_VOLUME_STITCHING = """System: 你是一位严谨的小说主编，擅长从宏观层面审核“分卷大纲”的合理性。

User:
我有两个相邻的【卷级大纲草案】（Volume Drafts）。
你的任务是判断 **Draft B (后一部分)** 是否仅仅是 **Draft A (前一部分)** 的自然延伸？
换句话说，从宏观叙事角度看，它们是否应该被合并为同一个“卷”？

### 输入数据 (Input Data)

**Fragment A (上一卷的尾部情节):**
- Plot ID: {end_id_a}
- Title: {title_a}
- Summary: {summary_of_last_plot_of_A}

**Fragment B (下一卷的头部情节):**
- Plot ID: {start_id_b}
- Title: {title_b}
- Summary: {summary_of_first_plot_of_B}

### 判定标准 (Criteria)

**【应当合并 (Merge)】** - 只要满足以下任一条件：
1. **核心事件未完结**：Draft A 结束时，核心冲突（如大BOSS决战）尚未彻底结束，Draft B 紧接着讲述该战斗的结局或直接后果。
2. **地图/舞台未切换**：主角依然在同一个核心地图（副本）活动，且身份地位没有发生本质变化。
3. **目标一致性**：两者都在服务于同一个短期人生目标（例如：都在为了“驾驭第二只鬼”这一具体目标而努力）。

**【必须独立 (Split)】** - 只要满足以下任一条件（优先级高于合并）：
1. **地图发生根本性迁移**：例如从“大昌市”彻底转移到了“总部”或“灵异之地”。
2. **人生阶段发生质变**：例如从“普通人”变成了“驭鬼者”，或从“驭鬼者”变成了“异类”。
3. **时间跨度巨大**：两者之间存在明显的“几年后”或长期的休整期。

### 输出要求 (Output Requirement)
请仅输出一个标准的 JSON 字符串。不要包含任何 Markdown 格式（如 ```json），不要包含任何开场白。

格式示例：
{{
  "should_merge": true,
  "reason": "虽然Plot ID不同，但两者都在描述饿死鬼事件的收尾阶段，地点仍在大昌市，属于同一卷的内容。"
}}

实际输出 JSON:
"""

# 卷级信息生成
VOLUME_GENERATION_PROMPT = """
# Role
你是一位专门构建“小说世界观数据库”的资深架构师。

# Task
基于提供的【情节摘要列表】，请为这一“卷（Volume）”生成一份深度的宏观档案。
输入包含了本卷所有情节的摘要（从 Plot {start_plot_id} 到 Plot {end_plot_id}）。

# Input Data
{plot_summaries_text}

# Output Requirement (Strict JSON)
请输出一个标准 JSON 对象，严格对应以下字段结构：

{{
  "volume_title": "为这一卷拟定一个宏大的标题（如：‘鬼眼刑警’ 或 ‘饿死鬼之灾’）",
  "volume_summary": "一段 500-800 字的宏观摘要。不要纠结于细碎的战斗，要侧重于主角的人生轨迹、世界观的揭示以及**本卷发生的所有事件与涉及人物**。如果本卷的内容过多，可以超过800字，但请务必确保内容的完整性和连贯性,且不超过2000字。",
  "time_span": "本卷故事发生的时间跨度（如：‘三个月’，‘2018年夏季’，或 ‘修仙历3500-3600年’）。如果小说里没有明确的时间线，请使用情节跨度进行概述，且总长度不得超过50个字。"
}}

# Constraints
1. `volume_summary` 必须覆盖本卷的主线推进、关键事件、核心人物和阶段变化，不能只写高潮。
2. `volume_summary` 必须仅基于输入的情节摘要生成，不得编造。
3. `time_span` 若无法确定精确时间，请用相对阶段表述，且长度不得超过50个字。
4. 只输出合法 JSON，不要输出 Markdown，不要输出解释文本。
"""

CHARACTER_DIRTY_REVIEW_PROMPT = """
你是一个角色脏数据清洗引擎。你的任务是只判断“这个角色实体是否明显是无意义路人，应当删除”。

判断标准：
1. 只有在角色明显是路人、工具人、场景NPC、纯职位称呼、纯泛称、对主线几乎没有独立意义时，才标记为 yes。
2. 如果角色有明确专名、像正式姓名、像稳定外号、像后续还会出现的人物，或者你不确定，就标记为 no。
3. 宁可保留，也不要误删。
4. 只能依据输入数据判断，不要编造。
5. 只输出合法 JSON，不要输出 markdown，不要解释。

输出格式：
[
  {{"name": "角色名A", "NEED_DELETE": "yes"}},
  {{"name": "角色名B", "NEED_DELETE": "no"}}
]

待判断数据：
{items_json}
"""

CHARACTER_CANONICAL_REWRITE_PROMPT = """
你是一个严格保守的角色命名清洗引擎。

任务：
给定一批角色条目。每个条目只包含：
- item_id
- name
- aliases

你要对每个条目做两件事：
1. 仅从该条目已有的 `name + aliases` 候选词中，选出最可能作为正名的 `canonical_name`
2. 清洗 alias，仅保留仍应属于该角色的候选词

强约束：
1. `canonical_name` 只能从该条目现有候选词中选择，绝对不能发明新名字。
2. 如果存在明确、更正式、更稳定的人名，应优先选择它作为 `canonical_name`。
3. 如果没有实名，但存在清晰锚点的关系描述名，也可以保留，例如：
   - 楚子航的妈妈
   - 路明非的叔叔
4. 无锚点泛称不能保留为正名或 alias，例如：
   - 妈妈、爸爸、叔叔、婶婶、老板、护士长
5. 以下内容不能保留为 alias：
   - 纯关系称呼：哥哥、姐姐、师兄、师姐
   - 职位/身份标签：主席、会长、家主、老板、老师、教授、校长
   - 武器名、物品名、组织名、阵营名
   - 明显属于其他角色的人名
6. 对于“楚子航的妈妈”这类带清晰锚点的关系描述名，不要误删。
7. 宁可少保留，也不要把可疑候选词留在 alias 中。
8. 只输出合法 JSON 数组，不要输出 Markdown，不要解释。

输出格式：
[
  {{
    "item_id": "原始 item_id",
    "canonical_name": "从现有候选词中选出的正名",
    "aliases": ["清洗后保留的 alias"]
  }}
]

待处理数据：
{items_json}
"""

CHARACTER_GENERIC_REWRITE_PROMPT = """
你是一个严格保守的小说角色称呼锚点改写器。

任务：
给定一个危险角色称呼（无锚点关系称呼、无锚点职位称呼、无锚点泛称），结合章节上下文，将它处理成以下两种结果之一：
1. `rewrite`：改写成唯一、明确、有指向性的角色名
2. `drop`：证据不足，不能安全改写，因此不进入角色表

强规则：
1. 绝对禁止输出无锚点泛称作为最终角色名，例如：
   叔叔、婶婶、女王、秘书、女秘书、客人、老板、经理、大副、护士、男孩、研究人员
2. 若能唯一确定指向，优先改写成：
   - 实名，或
   - 带锚点的唯一描述名
3. 若无法唯一确定，必须输出 `drop`
4. 宁可丢弃，也不要误改
5. 不得编造输入中没有的关系或身份
6. 若已有 aliases 中存在更完整、更正式、更稳定的人名，优先使用它作为 `canonical_name`

输入数据：
{item_json}

输出格式：
{{
  "action": "rewrite|drop",
  "canonical_name": "若 action=rewrite，则填写最终唯一名称；否则为空字符串",
  "aliases": ["若 action=rewrite，可保留的非泛称别名；否则为空数组"],
  "reason": "一句话说明依据"
}}

只输出合法 JSON，不要输出 Markdown，不要解释。
"""

CHARACTER_GROUP_FINALIZE_PROMPT = """
你是一个极度严格的小说角色最终命名裁决器。

任务：
给定一组已经确认属于同一角色的候选名称与别名，请输出最终唯一正名与最终别名列表。

目标：
1. 选择组内最像真实全名、最正式、最稳定、最具唯一性的名称作为 `canonical_name`
2. 删除一切冗余、模糊、泛化、职位/关系噪声、括号噪声、简称噪声
3. 只保留确实不可替代、且稳定专属于该角色的别名
4. 如果删除其他候选后不会损失辨识度，则 `aliases` 必须为空数组

强规则：
1. `canonical_name` 只能从输入中已有候选名称里选择，绝对不能发明新名字。
2. `aliases` 也只能从输入中已有候选名称里选择，绝对不能发明新名字。
3. 如果存在更完整、更正式、更像真实全名的人名，必须优先选它作为 `canonical_name`。
4. 以下内容默认不能保留为 `aliases`：
   - `canonical_name` 的简称、子串、只保留名不保留姓的残缺形式
   - “名字 + 职位/关系/身份”混合噪声，例如：麻衣队长、麻衣（队长）
   - 泛称、职位称呼、关系称呼
   - 与 `canonical_name` 表达同一信息的重复变体
5. 只有当某个称呼无法被 `canonical_name` 替代，且在文本中稳定专属于该角色时，才允许保留为 alias。
6. 宁可少保留 alias，也不要保留模糊 alias。
7. 当更完整的全名已经存在时，像“麻衣”这类只保留名的简称通常应删除，不要保留为 alias。
8. 示例：
   - 输入候选：麻衣、麻衣队长、麻衣（队长）、酒德麻衣
   - 正确输出：`canonical_name = 酒德麻衣`，`aliases = []`
9. 如果组内没有明显实名，但存在稳定专属代号或带锚点的唯一描述名，也可以将它作为 `canonical_name`。

输出格式：
{{
  "canonical_name": "最终正名",
  "aliases": ["最终保留的别名"],
  "dropped_candidates": [
    {{
      "text": "被删除的候选",
      "reason": "删除原因"
    }}
  ],
  "reason": "为什么选择这个 canonical_name"
}}

输入数据：
{group_json}

只输出合法 JSON，不要输出 Markdown，不要解释。
"""

CHARACTER_MERGE_CANDIDATE_GROUP_PROMPT = """
你是一个保守的小说角色候选聚合召回引擎。

任务：
给定同一本书的一批角色条目。每个条目只包含：
- item_id
- name
- aliases

请输出若干“可能是同一个角色”的候选集合。

强规则：
1. 这里只做候选召回，不做最终是否聚合的决定。
2. 宁可漏召回，也不要大量误召回。
3. 只有在名字、别名、带锚点描述名、音译漂移、敬称变化等证据明显接近时，才放进同一候选集合。
4. 如果出现“同姓或同根 + 先生/太太/小姐/夫人 + 全名/简称”这类情况，应优先放进同一候选集合，后续系统会再做拆分。
5. 如果只是共享泛称、关系称呼、职位称呼、组织名、武器名、物品名，不要放进同一候选集合。
6. 输出的每个集合都必须至少包含 2 个 item_id。
7. 输出中允许集合之间存在重叠；后续系统会再处理。
8. 只输出合法 JSON 数组，不要输出 Markdown，不要解释。

输出格式：
[
  {{
    "item_ids": ["角色A的 item_id", "角色B的 item_id"],
    "reason": "一句话说明为何怀疑这些名字可能指向同一角色"
  }}
]

待召回数据：
{items_json}
"""

CHARACTER_MERGE_IDENTITY_SUMMARY_PROMPT = """
你是一位严格的小说角色身份判别分析师。

目标角色：{character_name}
角色别名：{aliases_json}

下面给出该角色按章节整理的证据，输入类型是：{source_mode_label}
每一条证据都明确标注了章节号。

输入数据：
{evidence_json}

任务：
1. 只提取一个用于判断“是否应与其他名字聚合”的临时身份摘要 `identity_summary`。
2. `identity_summary` 必须尽量详细，重点保留：
   - 角色身份
   - 与关键人物的关系
   - 能区分此角色的稳定信息
3. 不要提取“在故事中的作用”“叙事定位”这类内容。
4. 如果关系信息不完整，不要因此否定同一性；只有在证据里出现明显冲突时，才体现冲突。
5. 不得编造。

输出格式：
{{
  "identity_summary": "较详细的临时身份摘要"
}}

只输出合法 JSON，不要输出 Markdown，不要解释。
"""

CHARACTER_MERGE_GROUP_RESOLUTION_PROMPT = """
你是一个严格保守的角色聚合裁决器。

任务：
下面给出一个候选集合。集合中的名字彼此相关，但不一定都属于同一个角色。
请你根据每个条目的 `name`、`aliases` 和 `identity_summary`，把这个候选集合进一步拆分成最终分组。

判定规则：
1. 只有在证据足够明确时，才把多个条目分进同一组。
2. 只要仍存在明显歧义，就拆开，不要强行合并。
3. 关系网不同本身不是反证；只有当身份、关系、事实存在明显冲突时，才不能放进同一组。
4. 允许输出单成员组。
5. 不要发明新的 item_id。
6. 输出时必须覆盖输入中的全部 item_id；如果拿不准，就让它单独成组。

输出格式：
{{
  "resolved_groups": [
    {{
      "item_ids": ["item_id_1", "item_id_2"],
      "reason": "为什么这些条目应归为同一角色"
    }},
    {{
      "item_ids": ["item_id_3"],
      "reason": "为什么这个条目应单独保留"
    }}
  ]
}}

输入数据：
{group_json}

只输出合法 JSON，不要输出 Markdown，不要解释。
"""

CHARACTER_SECOND_PASS_GROUP_RESOLUTION_PROMPT = """
你是一个严格保守的第二轮角色补聚合裁决器。

任务：
下面给出一组在第一轮聚合后仍然残留、可能漏并的角色条目。
请你根据每个条目的 `name`、`aliases`、`identity_summary` 和 `evidence_snippets`，
把这个候选集合进一步拆分成最终分组。

判定规则：
1. 这是第二轮补聚合，重点处理“前名/后名”“简称/全名”“头衔/正式名”“职位称呼/本名”等第一轮容易漏掉的情况。
2. 如果 `identity_summary` 或 `evidence_snippets` 中存在明确桥接证据，例如“改名为”“又叫”“就是”“被称为”“自称”，应优先合并。
3. 单纯同姓、同家族、同组织、同神话体系，不足以证明是同一个角色；如果身份不同，必须拆开。
4. 只有在证据足够明确时，才把多个条目分进同一组。
5. 只要仍存在明显歧义，就拆开，不要强行合并。
6. 允许输出单成员组。
7. 不要发明新的 item_id。
8. 输出时必须覆盖输入中的全部 item_id；如果拿不准，就让它单独成组。

输出格式：
{
  "resolved_groups": [
    {
      "item_ids": ["item_id_1", "item_id_2"],
      "reason": "为什么这些条目应归为同一角色"
    },
    {
      "item_ids": ["item_id_3"],
      "reason": "为什么这个条目应单独保留"
    }
  ]
}

输入数据：
{group_json}

只输出合法 JSON，不要输出 Markdown，不要解释。
"""

CHARACTER_PROFILE_SLICE_PROMPT = """
你是一位严格的小说角色档案分析师。

目标角色：{character_name}
卷序号：{volume_index}
卷名：{volume_title}
窗口章节范围：{chapter_start}-{chapter_end}

输入数据是按章节排序的 JSON 数组。每个元素仅包含：
- chapter_index：章节号
- record_description：目标角色在该章的经历描述
- chapter_summary：该章的整体摘要

输入数据：
{chapters_json}

任务：
1. 只围绕目标角色分析，不要输出与目标角色无关的背景人物信息。
2. 提炼这个窗口中目标角色的阶段画像。
3. 只在能从输入中明确看出关系时，提取目标角色与其他角色的关系事件。
4. 每条关系事件都必须包含章节范围与证据章节。
5. 不得编造。

输出必须是合法 JSON，对应如下结构：
{{
  "profile_slice": {{
    "volume_index": {volume_index},
    "volume_title": "{volume_title}",
    "chapter_start": {chapter_start},
    "chapter_end": {chapter_end},
    "summary": "该窗口内目标角色的阶段总结",
    "stable_signals": ["可跨阶段复用的稳定特征"],
    "current_state_signals": ["这一窗口内的状态、处境、目标、能力或资源变化"],
    "key_events": ["关键事件或转折"]
  }},
  "relation_events": [
    {{
      "target_character": "关系对象角色名",
      "relation_type": "关系类型",
      "polarity": "positive|neutral|negative|mixed",
      "strength": "weak|medium|strong",
      "summary": "该关系在本窗口内的简要说明",
      "chapter_start": 0,
      "chapter_end": 0,
      "evidence_chapters": [0]
    }}
  ]
}}
"""

CHARACTER_PROFILE_IDENTITY_ROLE_PROMPT = """
你是一位严格的小说角色档案编辑。

目标角色：{character_name}
角色别名：{aliases_json}
首次出现章节：{first_chapter_index}
最后出现章节：{last_chapter_index}
记录总数：{record_count}

下面给出该角色按窗口整理后的阶段画像切片：
{profile_slices_json}

下面给出该角色经过关键章节正文精修后形成的卷级中间摘要：
{profile_volume_groups_json}

请只提取角色的整体身份总结与叙事定位。不要输出其他字段，不要长篇展开，不得编造。

输出必须是合法 JSON，对应如下结构：
{{
  "identity": {{
    "summary": "角色整体身份与定位总结，控制在120字以内",
    "aliases": ["别名1", "别名2"],
    "first_chapter_index": {first_chapter_index},
    "last_chapter_index": {last_chapter_index}
  }},
  "narrative_role": ["最多5条叙事定位标签"]
}}
"""

CHARACTER_PROFILE_PERSONALITY_PROMPT = """
你是一位严格的小说角色画像编辑。

目标角色：{character_name}

下面给出该角色经过关键章节正文精修后形成的卷级中间摘要：
{profile_volume_groups_json}

请只提取角色的长期性格、行为风格、说话风格，以及长期稳定特征。不要输出其他字段，不得编造。

输出必须是合法 JSON，对应如下结构：
{{
  "personality_and_style": ["最多6条，单条尽量精炼"],
  "stable_profile": ["最多5条长期稳定特征"]
}}
"""

CHARACTER_PROFILE_APPEARANCE_PROMPT = """
你是一位严格的小说角色外貌设定编辑。

目标角色：{character_name}

下面给出该角色经过关键章节正文精修后形成的卷级中间摘要：
{profile_volume_groups_json}

请只提取角色的外貌、装束、可识别视觉特征。优先保留长期稳定、可直接观察到的描述；只有在文本证据明确时，才保留阶段性的外貌变化。不要输出气质评价、性格判断或能力信息，不得编造。

输出必须是合法 JSON，对应如下结构：
{{
  "appearance": ["最多6条外貌与装束特征"]
}}
"""

CHARACTER_PROFILE_MECHANISM_PROMPT = """
你是一位严格的小说角色机制分析编辑。

目标角色：{character_name}

下面给出该角色经过关键章节正文精修后形成的卷级中间摘要：
{profile_volume_groups_json}

请只提取角色的目标与动机、立场与阵营、能力与资源。不要输出其他字段，不得编造。

输出必须是合法 JSON，对应如下结构：
{{
  "goals_and_motivation": ["最多5条"],
  "stance_and_alignment": ["最多5条"],
  "abilities_and_resources": ["最多6条"]
}}
"""

CHARACTER_PROFILE_VOLUME_ARC_PROMPT = """
你是一位严格的小说角色卷级弧线编辑。

目标角色：{character_name}

下面给出该角色按窗口整理后的阶段画像切片：
{profile_slices_json}

下面给出该角色经过关键章节正文精修后形成的卷级中间摘要：
{profile_volume_groups_json}

请只输出按卷整理后的角色弧线。每卷内容必须精炼，不得编造。

输出必须是合法 JSON，对应如下结构：
{{
  "volume_arc": [
    {{
      "volume_index": 0,
      "volume_title": "卷名",
      "summary": "该卷角色总结，控制在100字以内",
      "role_in_volume": ["最多3条"],
      "goals": ["最多3条"],
      "state_changes": ["最多3条"],
      "relationship_changes": ["最多3条"]
    }}
  ]
}}
"""

CHARACTER_PROFILE_CURRENT_STATE_PROMPT = """
你是一位严格的小说角色当前状态编辑。

目标角色：{character_name}

下面给出该角色经过关键章节正文精修后形成的卷级中间摘要：
{recent_profile_volume_groups_json}

下面给出该角色全局卷级中间摘要：
{all_profile_volume_groups_json}

请只提取角色当前状态、关键转折、代表事件。不要输出其他字段，不得编造。

输出必须是合法 JSON，对应如下结构：
{{
  "current_state": ["最多6条当前状态"],
  "turning_points": ["最多6条关键转折"],
  "key_events": ["最多8条代表事件"]
}}
"""

CHARACTER_PROFILE_CRITICAL_CHUNK_PROMPT = """
你是一位严格的小说角色正文精修分析师。

目标角色：{character_name}
卷序号：{volume_index}
卷名：{volume_title}
正文精修批次章节：{chapter_start}-{chapter_end}

下面给出这个批次的关键章节正文精修包：
{critical_chapters_json}

任务：
1. 只围绕目标角色分析。
2. 从正文中提取更高维度的角色信号，不要输出无关背景信息。
3. 不要直接产出最终画像，只产出这个批次的高维信号。
4. 不得编造。

输出必须是合法 JSON，对应如下结构：
{{
  "volume_index": {volume_index},
  "volume_title": "{volume_title}",
  "chapter_start": {chapter_start},
  "chapter_end": {chapter_end},
  "summary": "该批次正文精修总结",
  "narrative_role_signals": ["叙事定位信号"],
  "personality_and_style_signals": ["性格、行为、说话风格信号"],
  "appearance_signals": ["外貌、装束、可识别视觉特征信号"],
  "goals_and_motivation_signals": ["目标与动机信号"],
  "stance_and_alignment_signals": ["立场与阵营信号"],
  "abilities_and_resources_signals": ["能力与资源信号"],
  "turning_point_signals": ["关键转折信号"],
  "important_relationship_signals": ["与其他重要角色相关的信号"],
  "evidence_chapters": [0]
}}
"""

CHARACTER_PROFILE_VOLUME_GROUP_PROMPT = """
你是一位严格的小说角色卷级聚合编辑。

目标角色：{character_name}
卷序号：{volume_index}
卷名：{volume_title}

下面给出该卷内多个正文精修批次结果：
{profile_chunks_json}

请将这些正文精修批次结果聚合为该卷的中间画像摘要。不要编造。

输出必须是合法 JSON，对应如下结构：
{{
  "volume_index": {volume_index},
  "volume_title": "{volume_title}",
  "summary": "该卷的高维角色摘要",
  "role_in_volume": ["该卷中的角色定位"],
  "goals": ["该卷中的主要目标"],
  "state_changes": ["该卷中的状态变化"],
  "relationship_changes": ["该卷中的关系变化"],
  "narrative_role_signals": ["叙事定位信号"],
  "personality_and_style_signals": ["性格风格信号"],
  "appearance_signals": ["外貌与装束信号"],
  "goals_and_motivation_signals": ["目标动机信号"],
  "stance_and_alignment_signals": ["立场阵营信号"],
  "abilities_and_resources_signals": ["能力资源信号"],
  "turning_point_signals": ["关键转折信号"]
}}
"""

CHARACTER_RELATION_OVERVIEW_PROMPT = """
你是一位严格的小说人物关系总览编辑。

目标角色：{character_name}
关系对象：{target_character_name}

下面给出这条人物关系按章节范围整理后的历史记录：
{history_json}

下面给出这条人物关系经过正文精修后形成的卷级中间摘要：
{relation_volume_groups_json}

请只输出这条关系的整体概括与当前状态，不要输出其他字段，不得编造。

输出必须是合法 JSON，对应如下结构：
{{
  "summary": "整体关系总结，控制在120字以内",
  "current_status": "当前关系状态"
}}
"""

CHARACTER_RELATION_STRUCTURE_PROMPT = """
你是一位严格的小说人物关系结构编辑。

目标角色：{character_name}
关系对象：{target_character_name}

下面给出这条人物关系经过正文精修后形成的卷级中间摘要：
{relation_volume_groups_json}

请只提取结构关系与行动关系，不要输出其他字段，不得编造。

输出必须是合法 JSON，对应如下结构：
{{
  "structural_relation": ["最多5条结构关系"],
  "action_relation": ["最多6条行动关系"]
}}
"""

CHARACTER_RELATION_DYNAMICS_PROMPT = """
你是一位严格的小说人物关系动力编辑。

目标角色：{character_name}
关系对象：{target_character_name}

下面给出这条人物关系按章节范围整理后的历史记录：
{history_json}

下面给出这条人物关系经过正文精修后形成的卷级中间摘要：
{relation_volume_groups_json}

请只提取情感关系、方向性、稳定度和驱动因素，不要输出其他字段，不得编造。

输出必须是合法 JSON，对应如下结构：
{{
  "emotional_relation": ["最多6条情感关系"],
  "directionality": "方向性",
  "stability": "稳定度",
  "drivers": ["最多5条驱动因素"]
}}
"""

CHARACTER_RELATION_HISTORY_SEGMENT_PROMPT = """
你是一位严格的小说人物关系阶段编辑。

目标角色：{character_name}
关系对象：{target_character_name}

下面给出当前需要精修的一段关系历史：
{history_segment_json}

下面给出与这段关系最相关的卷级中间摘要：
{relation_volume_groups_json}

请只输出这一段历史的精修结果，不要输出其他历史段，不要编造。

输出必须是合法 JSON，对应如下结构：
{{
  "history_segment": {{
    "chapter_start": 0,
    "chapter_end": 0,
    "relation_type": "关系类型",
    "structural_relation": ["该阶段的结构关系"],
    "action_relation": ["该阶段的行动关系"],
    "emotional_relation": ["该阶段的情感关系"],
    "polarity": "positive|neutral|negative|mixed",
    "strength": "weak|medium|strong",
    "directionality": "该阶段谁更主动或更依赖",
    "stability": "该阶段关系稳定度",
    "current_status": "该阶段关系状态",
    "drivers": ["该阶段关系驱动"],
    "summary": "该范围内的关系说明",
    "evidence_chapters": [0]
  }}
}}
"""

CHARACTER_RELATION_CRITICAL_CHUNK_PROMPT = """
你是一位严格的小说人物关系正文精修分析师。

目标角色：{character_name}
关系对象：{target_character_name}
卷序号：{volume_index}
卷名：{volume_title}
正文精修批次章节：{chapter_start}-{chapter_end}

下面给出这条关系的关键章节正文精修包：
{critical_chapters_json}

请只输出该批次关系的高维信号，不要直接输出最终关系模型。不要编造。

输出必须是合法 JSON，对应如下结构：
{{
  "volume_index": {volume_index},
  "volume_title": "{volume_title}",
  "chapter_start": {chapter_start},
  "chapter_end": {chapter_end},
  "summary": "该批次关系正文精修总结",
  "structural_relation_signals": ["结构关系信号"],
  "action_relation_signals": ["行动关系信号"],
  "emotional_relation_signals": ["情感关系信号"],
  "directionality_signals": ["方向性信号"],
  "stability_signals": ["稳定度信号"],
  "current_status_signals": ["当前状态信号"],
  "drivers": ["驱动因素"],
  "history_candidates": [
    {{
      "chapter_start": 0,
      "chapter_end": 0,
      "summary": "该范围内关系变化候选"
    }}
  ],
  "evidence_chapters": [0]
}}
"""

CHARACTER_RELATION_VOLUME_GROUP_PROMPT = """
你是一位严格的小说人物关系卷级聚合编辑。

目标角色：{character_name}
关系对象：{target_character_name}
卷序号：{volume_index}
卷名：{volume_title}

下面给出该卷内多个关系正文精修批次结果：
{relation_chunks_json}

请将这些批次聚合为该卷的中间关系摘要。不要编造。

输出必须是合法 JSON，对应如下结构：
{{
  "volume_index": {volume_index},
  "volume_title": "{volume_title}",
  "summary": "该卷的关系中间摘要",
  "structural_relation": ["结构关系"],
  "action_relation": ["行动关系"],
  "emotional_relation": ["情感关系"],
  "directionality": "方向性",
  "stability": "稳定度",
  "current_status": "当前状态",
  "drivers": ["驱动因素"],
  "history_json": [
    {{
      "chapter_start": 0,
      "chapter_end": 0,
      "relation_type": "关系类型",
      "summary": "该阶段关系总结",
      "evidence_chapters": [0]
    }}
  ]
}}
"""

CHARACTER_ROLEPLAY_RELATION_BATCH_PROMPT = """
你是一位严格的小说人物情感关系分析师。

目标角色：{character_name}
章节范围：{chapter_start}-{chapter_end}

下面给出这一批次的章节信息。
每个元素包含：
- chapter_index：章节号
- record_description：目标角色在该章的聚合记录
- chapter_summary：该章摘要
- chapter_content_excerpt：与目标角色相关的正文片段（可能为空）

输入数据：
{chapters_json}

任务：
1. 只分析与目标角色有关、具有明确情感指向的关系候选。
2. 主关系类型只能保留以下三类之一（每类只保留一个关键词，如“爱慕”）：
   - 爱慕/暧昧/恋爱
   - 仇恨/敌对
   - 朋友/亲人/父母
3. 不预设任何关系倾向。必须优先提取可观察到的事实、互动与情绪信号，再谨慎判断主关系类型。
4. 如果证据只支持“特殊关注、牵挂、信任、依赖、警惕、敬畏”等中性或弱结论，就停留在这些观察，不要主动升级为更强的关系判断。
5. 只有在文本证据足够明确时，才允许输出“爱慕/暧昧/恋爱”或“仇恨/敌对”。
6. 明显路人、普通合作、一般同事、完全无情感指向的关系不要输出。
7. 每条关系都必须有明确对象角色名。
8. 每条关系都必须给出章节范围和证据章节。
9. 不得编造。
10. 只输出合法 JSON，不要输出解释，不要输出 Markdown。

输出格式：
{{
  "emotional_relation_candidates": [
    {{
      "target_character": "角色名",
      "primary_relation_type": "爱慕/暧昧/恋爱|仇恨/敌对|朋友/兄弟",
      "emotional_signals": ["细情感信号"],
      "interaction_signals": ["支撑该判断的互动信号"],
      "explicitness": "explicit|implicit",
      "confidence": "low|medium|high",
      "intensity": "weak|medium|strong",
      "chapter_start": 0,
      "chapter_end": 0,
      "evidence_chapters": [0],
      "summary": "这一批次内两人的情感关系说明"
    }}
  ]
}}
"""

CHARACTER_ROLEPLAY_RELATION_SUMMARY_PROMPT = """
你是一位严格的小说人物情感关系主编。

目标角色：{character_name}
关系对象：{target_character_name}

下面给出这对角色在多个批次中抽取到的情感关系候选：
{relation_candidates_json}

任务：
1. 汇总这对角色的整体情感关系。
2. 主关系类型只能保留以下三类之一（每类只保留一个关键词，如“爱慕”）：
   - 爱慕/暧昧/恋爱
   - 仇恨/敌对
   - 朋友/亲人/父母
3. 不要为了完整性而强行拔高为爱慕、暧昧或仇恨。若证据不足，应优先保守归类。
4. 在主关系类型之外，只额外保留少量“次级情感倾向”，例如：牵挂、依赖、敬畏、警惕、感激等。
5. 只有当多个阶段反复出现、且证据明确时，才把“爱慕/暧昧/恋爱”作为主关系类型或次级倾向。
6. 尽量保留多个阶段的时间线，不要把不同阶段粗暴压成单段。
7. 如果关系经历了变化，要明确写出变化方向。
8. 不得编造。
9. 只输出合法 JSON，不要输出解释，不要输出 Markdown。
10. `target_character` 必须固定输出为 `{target_character_name}`，不要写成其他角色名。
11. 这里输出的是最终汇总结果，不要把中间推理信号全部带出来。
12. 所有数组都要克制：
   - `secondary_emotional_tendencies` 最多4条
   - `timeline` 最多6段
13. 每段 `timeline` 只保留章节范围和一句阶段概括，不要输出额外细字段。

输出格式：
{{
  "target_character": "角色名",
  "relation_summary": "两人的整体情感关系总结。",
  "primary_relation_type": "爱慕/暧昧/恋爱|仇恨/敌对|朋友/兄弟",
  "secondary_emotional_tendencies": ["次级情感倾向"],
  "intensity": "weak|medium|strong",
  "current_status": "当前关系状态",
  "timeline": [
    {{
      "chapter_start": 0,
      "chapter_end": 0,
      "summary": "该阶段关系概括"
    }}
  ]
}}
"""
