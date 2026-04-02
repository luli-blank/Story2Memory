# Story2Memory

Story2Memory 是一个面向本机使用场景的小说分析工作台，提供 Reflex Web UI，并通过 MySQL、Qdrant、Neo4j、Redis 和本地 rerank 服务支撑上传、分析与角色对话能力。

本仓库的公开目标是：

- 提供一个可公开发布、可一键 Docker 部署的版本
- 默认仅本机访问，开箱即安全
- 不附带任何小说正文、`epub`、封面图或其他受版权约束的数据
- 允许用户自行上传合法拥有使用权的 txt / epub 文件

代码以 [MIT](./LICENSE) 许可证发布。该许可证仅覆盖本仓库代码，不赋予任何第三方小说内容、封面或衍生数据的再分发权。

## Quickstart

```bash
cp .env.example .env
```

编辑 `.env`，至少替换以下示例值：

- `MYSQL_ROOT_PASSWORD`
- `MYSQL_PASSWORD`
- `NEO4J_PASSWORD`

然后启动：

```bash
docker compose up --build
```

默认访问地址：

- 前端 UI: `http://127.0.0.1:3000`
- Reflex backend / health endpoints: `http://127.0.0.1:8000`

默认仅本机访问。公开版不会默认开放局域网或公网访问。如果你需要对外暴露，请自行增加反向代理、鉴权和网络层访问控制。

## Runtime Notes

- 公开版默认 `AGENT_RUNTIME_PREWARM_ENABLED=0`，启动阶段不会主动预热外部 LLM 运行时。
- 应用可以在没有真实 LLM 调用的情况下完成“启动级”验证。
- 分析、聊天、角色扮演等模型相关功能，仍然需要你补齐真实的 LLM / Embedding 配置。

必填模型配置：

- `LLM_API_KEY`
- `LLM_BASE_URL`
- `LLM_MODEL`

如使用单独的 embedding 服务，还需要：

- `EMBED_API_KEY`
- `EMBED_BASE_URL`
- `EMBED_MODEL`

## Data Policy

- 本仓库不附带任何小说正文或演示书籍。
- 用户自行上传的数据仅应来自你明确有权处理的文本或电子书。
- 仓库不会为你生成或附送任何第三方版权内容。

## Public Repo Guarantees

- 公开版 Docker 入口固定为根目录 `Dockerfile`、`docker-compose.yml`、`.env.example`
- 默认端口仅绑定到 `127.0.0.1`
- 本地 `.env.*`、上传文件、书籍数据、封面图和工作缓存不会进入公开构建上下文
- GitHub Actions 会执行 `pytest -q` 和公开 Docker smoke test，作为合并门槛的一部分

## First Start Expectations

- 首次构建可能较慢，因为需要拉取基础镜像并为 rerank 服务准备模型缓存
- 如果 Docker Desktop / Docker Engine 未启动，`docker compose up --build` 会直接失败
- 如果你保留了 `.env.example` 中的占位密码，应用会在启动前快速失败并提示你修改

## Troubleshooting

- 打不开前端：检查 `docker compose ps`，确认 `app` 容器已启动，并访问 `http://127.0.0.1:3000`
- 后端健康检查失败：访问 `http://127.0.0.1:8000/_health` 和 `http://127.0.0.1:8000/ping`
- 模型功能不可用：确认 `LLM_*` 与 `EMBED_*` 变量已经替换为真实值
- 想做一次公开版启动验证：运行 `bash scripts/ci_public_smoke.sh`

## Development Checks

```bash
pytest -q
bash scripts/ci_public_smoke.sh
```
