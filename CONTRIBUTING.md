# Contributing

感谢你关注 Story2Memory。

## Before Opening a Pull Request

- 先确认变更不引入任何第三方版权正文、`epub`、封面图或私有配置
- 本地运行：
  - `pytest -q`
  - `bash scripts/ci_public_smoke.sh`
- 如果改动了公开部署契约，请同步更新 `README.md`、`.env.example` 和相关测试

## Scope

欢迎提交以下类型的改进：

- Docker 部署稳定性与安全性
- 文档与可用性
- 测试覆盖与 CI
- 小说分析和角色扮演体验改进

不接受：

- 受版权保护的小说正文、示例电子书、封面图
- 私有部署凭据、个人环境脚本或本地备份文件

## Pull Request Notes

- 保持改动聚焦
- 为行为变更补测试
- 不要提交 `.env`、`.env.*`、上传文件、工作缓存或数据目录内容
