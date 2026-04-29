# 微调实验指南

这份文档仅描述如何在本地导出的、用户自有版权或已获授权的数据上进行实验。公开仓库不附带训练数据、小说正文或封面素材。

## 数据来源

训练数据需要由部署者在本机生成，默认输出目录为：

```text
output/finetune_dataset/
```

该目录属于实验产物，已被 `.gitignore` 和 `.dockerignore` 排除，不应提交到公开仓库。

## 任务定义

建议先从最小闭环开始：

- 输入：`chapter_summary`、`previous_text_tail`
- 可选输入：`plot_summary`、`volume_summary`
- 输出：`target_text`

目标不是复刻任何第三方作品，而是验证“基于摘要生成长篇叙事片段”的数据管线、训练脚本和评估流程。

## 导出数据

```bash
python scripts/export_finetune_dataset.py
```

常用参数请参考 [finetune_dataset.md](./finetune_dataset.md)。

## 实验建议

1. 先抽样检查数据，确认不包含未授权内容。
2. 先跑小样本训练，确认格式、loss、保存和推理链路可用。
3. 再比较不同输入字段组合，例如“章节摘要”与“章节摘要 + 剧情摘要”。
4. 固定一小批人工评测样本，用于观察连贯性、摘要一致性、重复和跑题问题。

## 发布边界

公开仓库只保留代码、脚本和文档模板。任何由用户上传书籍导出的 `jsonl`、模型权重、检查点、日志和评测样本都属于本地产物，不进入 Git，也不进入 Docker 构建上下文。
