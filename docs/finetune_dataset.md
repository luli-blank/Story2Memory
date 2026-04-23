# 微调数据集导出

这个项目已经具备组织“摘要 -> 章节正文”训练样本的核心字段：

- `book_chapters.content`：章节原文
- `book_chapters.chapter_summary` / `raw_summary_json`：章节级摘要
- `book_plots.plot_summary`：情节级摘要
- `book_volumes.volume_summary`：卷级摘要

对应表结构见 [database/mysql/create_tables.sql](../database/mysql/create_tables.sql)。

## 导出脚本

脚本路径：

```bash
python scripts/export_finetune_dataset.py
```

默认输出到：

```text
output/finetune_dataset/
```

会生成：

- `train.raw.jsonl`
- `train.messages.jsonl`
- `validation.raw.jsonl`（如果存在验证集）
- `validation.messages.jsonl`（如果存在验证集）
- `manifest.json`

## 切分策略

默认按“整本书”切分训练集和验证集，而不是按章节随机切分。

这样做的原因：

- 避免同一本书的相邻章节同时出现在 train / validation 中
- 避免模型在验证集里看到几乎连续的上下文，导致评估虚高
- 更接近真实场景：给模型一本未见过的同作者作品或一本书中的独立留出部分

## 导出示例

导出全部书籍，20% 书本级验证集：

```bash
python scripts/export_finetune_dataset.py \
  --eval-ratio 0.2 \
  --seed 42
```

只导出指定书籍：

```bash
python scripts/export_finetune_dataset.py \
  --book-ids 1,2,3
```

显式指定训练集 / 验证集书籍：

```bash
python scripts/export_finetune_dataset.py \
  --train-book-ids 1,2 \
  --validation-book-ids 3
```

调大连续上下文尾巴长度：

```bash
python scripts/export_finetune_dataset.py \
  --previous-tail-chars 1000
```

## 样本格式

`*.raw.jsonl` 每条记录包含：

- `conditioning`：卷级摘要、情节摘要、章节摘要、上一段末尾
- `instruction`：可直接拿来做监督微调 prompt 的用户指令
- `target_text`：目标正文

`*.messages.jsonl` 是对话格式：

- `system`
- `user`
- `assistant`

更适合直接喂给对话式 SFT 管线。

## 当前假设

当前版本默认把 `book_chapters` 的每一条记录视为一个训练片段。  
如果原始章节过长，项目在入库前已经可能将其切成多个片段，见 [rag/bookSlice.py](../rag/bookSlice.py)。

## 建议

如果目标是“学习作者风格”，建议分两层训练：

1. 先用同作者原文做续写式训练，吸收语言节奏和文笔。
2. 再用这里导出的精简摘要样本做受控生成微调，学习按提纲写正文。

单纯使用“章节摘要 -> 正文”做微调，通常更容易学到“扩写摘要”，而不一定稳定学到作者文风。
