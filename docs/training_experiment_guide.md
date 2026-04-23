# 章节生成微调实验指南

这份文档是给“另一个 code agent”的工作说明。目标不是让它重新理解整个项目，而是让它基于当前已经准备好的数据，直接进入训练方案设计、训练脚本搭建、实验执行与评估。

## 1. 当前任务目标

请基于当前项目中已经导出的数据集，指导并落地一个“根据摘要生成章节正文”的训练实验。

更准确地说：

- 输入：`chapter_summary` + `previous_text_tail`
- 可选输入：`plot_summary`、`volume_summary`
- 输出：对应的章节正文 `target_text`

当前目标不是“精确还原原文”，而是训练出一个能够：

- 根据摘要稳定扩写正文
- 在长篇小说语境下保持叙事连贯
- 尽量学习江南作品的写作节奏和表达风格

## 2. 当前数据状态

当前已经生成好的数据位于：

- [output/finetune_dataset/train.raw.jsonl](/Users/luli/programe/story2memory/output/finetune_dataset/train.raw.jsonl:1)
- [output/finetune_dataset/train.messages.jsonl](/Users/luli/programe/story2memory/output/finetune_dataset/train.messages.jsonl:1)
- [output/finetune_dataset/validation.raw.jsonl](/Users/luli/programe/story2memory/output/finetune_dataset/validation.raw.jsonl:1)
- [output/finetune_dataset/validation.messages.jsonl](/Users/luli/programe/story2memory/output/finetune_dataset/validation.messages.jsonl:1)
- [output/finetune_dataset/manifest.json](/Users/luli/programe/story2memory/output/finetune_dataset/manifest.json:1)

当前书级切分已经固定为：

- 训练集：`book_id=8`
- 验证集：`book_id=7`

也就是说：

- `train` 使用《龙族全套 共6册（龙族1-4）》
- `validation` 使用《龙族V·悼亡者的归来 网络连载版》

这是一个“同作者跨作品验证”的设置，适合检验模型是否学到了可迁移的写作方式，而不是只记住训练书中的局部内容。

## 3. 当前样本格式

### 3.1 raw 格式

`*.raw.jsonl` 每条记录包含：

```json
{
  "record_type": "story_segment_sft",
  "split": "train",
  "sample_id": "8:123",
  "conditioning": {
    "volume_summary": "...",
    "plot_summary": "...",
    "chapter_summary": "...",
    "previous_text_tail": "..."
  },
  "target_text": "...",
  "instruction": "..."
}
```

### 3.2 messages 格式

`*.messages.jsonl` 每条记录包含：

```json
{
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ]
}
```

## 4. 当前数据约束

本次实验请严格基于当前这套数据，不要擅自回到旧版“大而全”的结构化字段条件集。

当前保留字段只有：

- 必要：
  - `chapter_summary`
  - `previous_text_tail`
  - `target_text`
- 可选：
  - `plot_summary`
  - `volume_summary`

不要把以下字段重新加回训练输入，除非实验明确要求对照：

- 角色结构化列表
- 世界规则
- 组织势力
- 特殊存在
- 其它复杂 metadata

原因：

- 当前实验优先验证“简洁条件是否足够”
- 条件过多会让模型变成结构化填空器，不利于观察真正的写作能力和风格泛化

## 5. 对训练 agent 的明确任务

请完成以下事情：

1. 选择一个合适的开源中文/多语基座模型，优先考虑指令跟随和长文本生成稳定性。
2. 优先采用参数高效微调方案，如 LoRA / QLoRA。
3. 先搭一个最小可复现实验，而不是一上来做复杂训练矩阵。
4. 给出训练脚本、依赖、运行命令和输出目录约定。
5. 给出评估脚本或评估方法，至少覆盖：
   - loss / perplexity 类指标
   - 摘要一致性
   - 连贯性
   - 风格接近度的人工检查方案
6. 明确说明哪些指标可信，哪些只能作参考。
7. 如果发现数据中仍有明显噪声样本，请先统计再决定是否清洗，不要直接大规模改数据。

## 6. 建议实验顺序

请按下面顺序推进，而不是一次性铺太大。

### 实验 A：最小基线

只使用：

- `chapter_summary`
- `previous_text_tail`

目标：

- 验证最小输入是否足以生成可读正文
- 观察模型是否会严重跑题、重复、空泛

### 实验 B：增强摘要条件

在实验 A 的基础上加入：

- `plot_summary`
- `volume_summary`

目标：

- 比较多级摘要是否提升长程一致性
- 验证是否会因为条件过多而损伤自然表达

### 实验 C：输入模板 ablation

对比两种 prompt 组织方式：

1. 自然语言段落式
2. 显式字段标签式

目标：

- 看哪一种对训练稳定性和生成质量更好

### 实验 D：继续训练 vs 纯 SFT

如果资源允许，可以增加一个小实验：

- 方案 1：直接对指令模型做 SFT
- 方案 2：先做少量原文续写适配，再做 SFT

目标：

- 判断“先学文风，再学受控生成”是否更有效

## 7. 推荐训练原则

### 7.1 不要过早追求大模型

优先选择能快速迭代的模型尺寸。  
先把实验闭环打通，比一开始冲大模型更重要。

### 7.2 优先保证 target 长度分布可控

请先统计：

- `target_text` 字符长度分布
- `instruction` 长度分布
- 总 token 分布

如果训练序列过长，要优先决定：

- 截断策略
- packing 策略
- 是否只训练 assistant 输出部分

### 7.3 保持验证集干净

当前验证集是整本 `book_id=7`。  
不要再从训练书里抽章节混入验证集。

### 7.4 先建立人工评测集

请从验证集抽一小批固定样本，形成一个稳定的人工评估子集，例如 20 到 50 条。  
后续每轮实验都在同一批样本上对比输出。

## 8. 建议重点评估维度

请不要只看训练 loss。

至少从下面几个角度评估：

### 8.1 摘要一致性

检查生成正文是否覆盖并遵守 `chapter_summary` 中的关键信息。

重点看：

- 事件是否跑偏
- 是否新增摘要里没有的核心情节
- 是否遗漏摘要中明确指出的事件

### 8.2 连贯性

检查 `previous_text_tail` 是否真的帮助模型完成衔接。

重点看：

- 开头是否接得上
- 人称、时态、动作是否连续
- 是否出现突兀跳转

### 8.3 风格感

这部分建议人工评估，不要过分相信自动指标。

重点看：

- 句子长短节奏
- 对话感
- 描写密度
- 网文叙事推进感

### 8.4 重复与空话

重点排查：

- 同义反复
- 摘要复述式正文
- 大量泛化形容词
- 缺少场景与动作支撑

## 9. 需要警惕的风险

### 9.1 数据中仍可能有非正文样本

虽然当前数据已经收缩，但训练样本里可能仍混有：

- 前言
- 目录
- 出版说明
- 非叙事文本

请先抽样统计，再决定是否增加过滤。

### 9.2 样本任务本质是一对多

同一摘要可能对应多种合理写法。  
所以：

- 验证集 loss 不能等价代表“写得好”
- 自动指标只能辅助，不能替代人工检查

### 9.3 容易学成“摘要改写器”

如果模型只会把摘要重新说一遍，而无法自然展开场景和动作，说明训练目标达成得不够。

### 9.4 容易过拟合具体书内设定

训练集和验证集虽然是不同作品，但仍是同作者同系列。  
验证通过不代表模型已经具备普遍化写作能力。

## 10. 建议输出物

请训练 agent 最终至少交付：

1. 训练方案说明
2. 依赖安装方式
3. 训练脚本
4. 推理 / 采样脚本
5. 验证脚本或验证说明
6. 一份实验记录模板
7. 一份对比表，至少包含：
   - 模型名
   - 输入字段组合
   - 序列长度
   - 学习率
   - epoch / steps
   - 验证 loss
   - 主观评价结论

## 11. 建议你优先采用的第一轮方案

如果需要一个默认起点，请直接从这里开始：

- 基座模型：选择一个中等尺寸、中文生成能力稳定的 instruct 模型
- 微调方式：LoRA
- 数据格式：优先使用 `messages.jsonl`
- 第一轮输入：只保留 `chapter_summary + previous_text_tail`
- 第二轮输入：再加入 `plot_summary + volume_summary`
- 验证方式：
  - 看 validation loss
  - 固定抽样 20 到 50 条做人工对比

## 12. 额外说明

如果训练 agent 想修改数据导出逻辑，必须先说明理由，再改。  
当前优先级不是继续折腾数据结构，而是先用现有数据把实验闭环跑通。

如果训练 agent 认为需要重新清洗数据，请它先做：

1. 噪声样本定义
2. 噪声样本统计
3. 清洗前后样本量变化
4. 对实验结论可能造成的影响

不要直接在没有统计的情况下重写数据集。
