from __future__ import annotations

import reflex as rx

from ..state import NovelState, StartupServiceStatus
from .common import glass_panel


def _readonly_value(label: str, value: str, *, description: str = "") -> rx.Component:
    return rx.vstack(
        rx.text(label, color="#f8fafc", font_weight="700", font_size="0.86rem", letter_spacing="0.02em"),
        rx.cond(
            description,
            rx.text(description, color="#7dd3fc", font_size="0.74rem", line_height="1.6"),
            rx.box(),
        ),
        rx.box(
            rx.text(
                value,
                color="#e2e8f0",
                font_size="0.82rem",
                font_family="monospace",
                line_height="1.65",
                white_space="normal",
                word_break="break-all",
            ),
            padding="0.9rem 1rem",
            border_radius="14px",
            background="rgba(5, 10, 20, 0.72)",
            border="1px solid rgba(96, 165, 250, 0.18)",
            width="100%",
            box_shadow="inset 0 1px 0 rgba(255,255,255,0.04)",
        ),
        align="start",
        spacing="1",
        width="100%",
    )


def _config_input(
    label: str,
    value,
    on_change,
    placeholder: str,
    *,
    input_type: str = "text",
    description: str = "",
) -> rx.Component:
    return rx.vstack(
        rx.text(label, color="#f8fafc", font_weight="700", font_size="0.86rem", letter_spacing="0.02em"),
        rx.cond(
            description,
            rx.text(description, color="#7dd3fc", font_size="0.74rem", line_height="1.6"),
            rx.box(),
        ),
        rx.input(
            value=value,
            on_change=on_change,
            placeholder=placeholder,
            type=input_type,
            border_radius="14px",
            background="rgba(5, 10, 20, 0.72)",
            border="1px solid rgba(96, 165, 250, 0.18)",
            color="white",
            width="100%",
            height="3rem",
            box_shadow="inset 0 1px 0 rgba(255,255,255,0.04)",
        ),
        align="start",
        spacing="1",
        width="100%",
    )


def _service_badge(item: StartupServiceStatus) -> rx.Component:
    return rx.badge(
        rx.cond(
            item.status == "ready",
            "READY",
            rx.cond(item.status == "starting", "STARTING", "FAILED"),
        ),
        color_scheme=rx.cond(
            item.status == "ready",
            "green",
            rx.cond(item.status == "starting", "amber", "red"),
        ),
        variant="soft",
        size="2",
    )


def _service_row(item: StartupServiceStatus) -> rx.Component:
    return rx.box(
        rx.hstack(
            rx.vstack(
                rx.hstack(
                    rx.text(item.label, color="#f8fafc", font_weight="700", font_size="0.95rem"),
                    rx.cond(
                        item.blocking,
                        rx.box(
                            rx.text(
                                "blocking",
                                color="#cbd5e1",
                                font_size="0.62rem",
                                letter_spacing="0.12em",
                                text_transform="uppercase",
                            ),
                            padding="0.18rem 0.42rem",
                            border_radius="999px",
                            border="1px solid rgba(148, 163, 184, 0.16)",
                            background="rgba(15, 23, 42, 0.55)",
                        ),
                        rx.box(
                            rx.text(
                                "optional",
                                color="#7dd3fc",
                                font_size="0.62rem",
                                letter_spacing="0.12em",
                                text_transform="uppercase",
                            ),
                            padding="0.18rem 0.42rem",
                            border_radius="999px",
                            border="1px solid rgba(125, 211, 252, 0.14)",
                            background="rgba(8, 47, 73, 0.28)",
                        ),
                    ),
                    spacing="2",
                    align="center",
                ),
                rx.text(item.detail, color="#94a3b8", font_size="0.8rem", line_height="1.7"),
                align="start",
                spacing="1",
            ),
            rx.spacer(),
            _service_badge(item),
            width="100%",
            align="start",
            spacing="3",
        ),
        padding="0.95rem 1rem",
        border_radius="16px",
        background="rgba(4, 9, 19, 0.58)",
        border=rx.cond(
            item.status == "ready",
            "1px solid rgba(34, 197, 94, 0.38)",
            rx.cond(
                item.status == "starting",
                "1px solid rgba(245, 158, 11, 0.34)",
                "1px solid rgba(244, 63, 94, 0.32)",
            ),
        ),
        box_shadow="inset 0 1px 0 rgba(255,255,255,0.03)",
        width="100%",
    )


def _capability_row(title: str, description: str, checked, on_change) -> rx.Component:
    return rx.hstack(
        rx.vstack(
            rx.text(title, color="#f8fafc", font_weight="700", font_size="0.95rem"),
            rx.text(description, color="#94a3b8", font_size="0.8rem", line_height="1.7"),
            align="start",
            spacing="1",
        ),
        rx.spacer(),
        rx.switch(checked=checked, on_change=on_change, color_scheme="cyan"),
        width="100%",
        align="center",
        spacing="3",
    )


def _required_row(title: str, description: str) -> rx.Component:
    return rx.hstack(
        rx.vstack(
            rx.text(title, color="#f8fafc", font_weight="700", font_size="0.95rem"),
            rx.text(description, color="#94a3b8", font_size="0.8rem", line_height="1.7"),
            align="start",
            spacing="1",
        ),
        rx.spacer(),
        rx.badge("REQUIRED", color_scheme="cyan", variant="soft", size="2"),
        width="100%",
        align="center",
        spacing="3",
    )


def startup_setup_view() -> rx.Component:
    return rx.box(
        rx.vstack(
            rx.box(
                rx.vstack(
                    rx.hstack(
                        rx.vstack(
                            rx.box(
                                rx.text(
                                    "ARK ONLY",
                                    color="#7dd3fc",
                                    font_size="0.7rem",
                                    font_weight="800",
                                    letter_spacing="0.18em",
                                ),
                                padding="0.25rem 0.52rem",
                                border_radius="999px",
                                border="1px solid rgba(125, 211, 252, 0.18)",
                                background="rgba(8, 47, 73, 0.28)",
                            ),
                            rx.text("首启配置向导", color="#f8fafc", font_size="2rem", font_weight="900"),
                            rx.text(
                                "Docker 依赖已内置。填写火山引擎 Ark Key 与模型后，先测试，再直接进入书架。",
                                color="#94a3b8",
                                font_size="0.95rem",
                                line_height="1.8",
                                max_width="48rem",
                            ),
                            align="start",
                            spacing="2",
                        ),
                        rx.spacer(),
                        rx.badge(
                            NovelState.startup_status_label,
                            color_scheme="cyan",
                            variant="soft",
                            size="3",
                        ),
                        width="100%",
                        align="start",
                        spacing="4",
                    ),
                    rx.hstack(
                        rx.box(
                            rx.text(
                                "运行时配置文件",
                                color="#64748b",
                                font_size="0.66rem",
                                letter_spacing="0.14em",
                                text_transform="uppercase",
                            ),
                            rx.text(
                                NovelState.startup_runtime_path_label,
                                color=NovelState.startup_status_accent,
                                font_size="0.8rem",
                                font_family="monospace",
                                line_height="1.6",
                            ),
                            padding="0.72rem 0.9rem",
                            border_radius="16px",
                            background="rgba(2, 6, 23, 0.45)",
                            border="1px solid rgba(96, 165, 250, 0.12)",
                            min_width="18rem",
                        ),
                        rx.box(
                            rx.text(
                                "进入条件",
                                color="#64748b",
                                font_size="0.66rem",
                                letter_spacing="0.14em",
                                text_transform="uppercase",
                            ),
                            rx.text(
                                rx.cond(
                                    NovelState.startup_test_is_fresh,
                                    "最近一次测试已通过",
                                    "当前输入尚未完成有效测试",
                                ),
                                color="#e2e8f0",
                                font_size="0.8rem",
                            ),
                            padding="0.72rem 0.9rem",
                            border_radius="16px",
                            background="rgba(2, 6, 23, 0.45)",
                            border="1px solid rgba(148, 163, 184, 0.12)",
                            min_width="18rem",
                        ),
                        width="100%",
                        spacing="3",
                        flex_wrap="wrap",
                    ),
                    rx.cond(
                        NovelState.startup_validation_errors,
                        rx.vstack(
                            rx.text("当前还缺少这些必需配置：", color="#fecdd3", font_size="0.84rem", font_weight="700"),
                            rx.foreach(
                                NovelState.startup_validation_errors,
                                lambda item: rx.text(f"• {item}", color="#fda4af", font_size="0.8rem"),
                            ),
                            align="start",
                            spacing="1",
                            width="100%",
                        ),
                        rx.box(),
                    ),
                    rx.cond(
                        NovelState.startup_feedback,
                        rx.box(
                            rx.text(
                                NovelState.startup_feedback,
                                color=rx.cond(NovelState.startup_feedback_is_error, "#fecdd3", "#dcfce7"),
                                font_size="0.84rem",
                                line_height="1.75",
                                white_space="pre-wrap",
                            ),
                            padding="0.88rem 1rem",
                            border_radius="16px",
                            background=rx.cond(
                                NovelState.startup_feedback_is_error,
                                "rgba(127, 29, 29, 0.26)",
                                "rgba(20, 83, 45, 0.24)",
                            ),
                            border=rx.cond(
                                NovelState.startup_feedback_is_error,
                                "1px solid rgba(251, 113, 133, 0.18)",
                                "1px solid rgba(74, 222, 128, 0.18)",
                            ),
                            width="100%",
                        ),
                        rx.box(),
                    ),
                    rx.cond(
                        NovelState.startup_last_saved_at,
                        rx.text(
                            f"最近应用：{NovelState.startup_last_saved_at}",
                            color="#64748b",
                            font_size="0.76rem",
                        ),
                        rx.box(),
                    ),
                    spacing="4",
                    align="start",
                    width="100%",
                ),
                width="100%",
                padding="1.4rem 1.5rem 1.55rem",
                border_radius="28px",
                border="1px solid rgba(125, 211, 252, 0.14)",
                background="linear-gradient(160deg, rgba(3, 7, 18, 0.96) 0%, rgba(15, 23, 42, 0.9) 56%, rgba(8, 47, 73, 0.78) 100%)",
                box_shadow="0 30px 80px rgba(2, 6, 23, 0.46)",
                position="relative",
                overflow="hidden",
            ),
            rx.grid(
                glass_panel(
                    rx.vstack(
                        rx.hstack(
                            rx.vstack(
                                rx.text("服务状态", color="#f8fafc", font_size="1.15rem", font_weight="800"),
                                rx.text(
                                    "检查本地 Docker 依赖是否已就绪。MySQL、Neo4j 与 App Backend 会直接阻塞首启。",
                                    color="#94a3b8",
                                    font_size="0.82rem",
                                    line_height="1.7",
                                ),
                                align="start",
                                spacing="1",
                            ),
                            rx.spacer(),
                            rx.button(
                                NovelState.startup_refresh_button_text,
                                on_click=NovelState.refresh_startup_service_statuses,
                                disabled=NovelState.startup_services_loading
                                | NovelState.startup_test_running
                                | NovelState.startup_apply_running,
                                background="rgba(15, 23, 42, 0.82)",
                                color="#e2e8f0",
                                border="1px solid rgba(148, 163, 184, 0.16)",
                                border_radius="12px",
                            ),
                            width="100%",
                            align="start",
                        ),
                        rx.foreach(NovelState.startup_service_statuses, _service_row),
                        spacing="3",
                        align="stretch",
                        width="100%",
                    ),
                    padding="1.15rem",
                    width="100%",
                    height="100%",
                ),
                glass_panel(
                    rx.vstack(
                        rx.text("Ark 配置", color="#f8fafc", font_size="1.15rem", font_weight="800"),
                        rx.text(
                            "V1 仅支持火山引擎 Ark。LLM、Embedding、Rerank 都是首启必需项；LLM 与 Embedding URL 固定展示。",
                            color="#94a3b8",
                            font_size="0.82rem",
                            line_height="1.7",
                        ),
                        _config_input(
                            "ARK_API_KEY",
                            NovelState.setup_ark_api_key,
                            NovelState.set_setup_ark_api_key,
                            "在这里粘贴火山引擎 Ark API Key",
                            input_type="password",
                            description="同一套 Ark Key 会同时用于对话模型和 Embedding 模型。",
                        ),
                        _config_input(
                            "LLM_MODEL",
                            NovelState.setup_llm_model,
                            NovelState.set_setup_llm_model,
                            "例如：doubao-seed-1-6-250615",
                            description="用于章节总结、问答与角色相关推理。下方 URL 为固定调用入口。",
                        ),
                        _readonly_value(
                            "LLM_BASE_URL",
                            "https://ark.cn-beijing.volces.com/api/coding/v3",
                            description="LLM 测试与运行时固定走 Ark Coding 网关。",
                        ),
                        _config_input(
                            "EMBED_MODEL",
                            NovelState.setup_embed_model,
                            NovelState.set_setup_embed_model,
                            "例如：doubao-embedding-large-text-250515",
                            description="向量检索必需模型。支持文本或多模态 embedding endpoint。",
                        ),
                        _readonly_value(
                            "EMBED_BASE_URL",
                            "https://ark.cn-beijing.volces.com/api/v3",
                            description="Embedding 测试与运行时固定走 Ark Embedding 网关。",
                        ),
                        spacing="3",
                        align="stretch",
                        width="100%",
                    ),
                    padding="1.15rem",
                    width="100%",
                    height="100%",
                ),
                columns="2",
                spacing="4",
                width="100%",
            ),
            glass_panel(
                rx.vstack(
                    rx.text("运行时要求", color="#f8fafc", font_size="1.15rem", font_weight="800"),
                    rx.text(
                        "向量检索与 Rerank 现在都是首启必需能力，测试通过后才能进入书架。",
                        color="#94a3b8",
                        font_size="0.82rem",
                        line_height="1.7",
                    ),
                    _required_row(
                        "向量检索",
                        "固定启用。会连接 Qdrant，并在上传与检索链路使用 Ark Embedding。",
                    ),
                    _required_row(
                        "Rerank 重排",
                        "固定启用。测试页会校验远程 rerank 服务，作为问答链路的必需精排步骤。",
                    ),
                    rx.vstack(
                        rx.grid(
                            _config_input(
                                "RERANK_PROVIDER",
                                NovelState.setup_rerank_provider,
                                NovelState.set_setup_rerank_provider,
                                "qwen / local / openai_compatible",
                                description="默认推荐 qwen。远程 DashScope 兼容接口请填写 qwen。",
                            ),
                            _config_input(
                                "RERANK_MODEL",
                                NovelState.setup_rerank_model,
                                NovelState.set_setup_rerank_model,
                                "qwen3-rerank",
                                description="Rerank 必需模型，用于对召回候选进行精排。",
                            ),
                            columns="2",
                            spacing="3",
                            width="100%",
                        ),
                        _config_input(
                            "RERANK_BASE_URL",
                            NovelState.setup_rerank_base_url,
                            NovelState.set_setup_rerank_base_url,
                            "https://dashscope.aliyuncs.com/compatible-api/v1/reranks",
                            description="Rerank 测试与运行时都直接请求这个 URL。",
                        ),
                        _config_input(
                            "RERANK_API_KEY",
                            NovelState.setup_rerank_api_key,
                            NovelState.set_setup_rerank_api_key,
                            "在这里粘贴 DashScope API Key",
                            input_type="password",
                            description="仅写入本地 runtime.env，不进入 Git。",
                        ),
                        _config_input(
                            "RERANK_INSTRUCTION",
                            NovelState.setup_rerank_instruction,
                            NovelState.set_setup_rerank_instruction,
                            "Given a web search query, retrieve relevant passages that answer the query.",
                            description="Qwen rerank 的 instruct，可保持默认。",
                        ),
                        padding="0.9rem",
                        border_radius="18px",
                        background="rgba(15, 23, 42, 0.42)",
                        border="1px solid rgba(96, 165, 250, 0.14)",
                        spacing="3",
                        align="stretch",
                        width="100%",
                    ),
                    _capability_row(
                        "启动期预热",
                        "启用后在应用加载时预热部分运行时客户端，减少首次检索延迟。",
                        NovelState.setup_prewarm_enabled,
                        NovelState.set_setup_prewarm_enabled,
                    ),
                    rx.box(
                        rx.text(
                            "流程固定为：刷新状态 -> 测试配置 -> 开始使用。当前输入变更后，之前的测试结果会立即失效。",
                            color="#7dd3fc",
                            font_size="0.78rem",
                            line_height="1.7",
                        ),
                        padding="0.8rem 0.95rem",
                        border_radius="16px",
                        background="rgba(8, 47, 73, 0.18)",
                        border="1px solid rgba(125, 211, 252, 0.12)",
                        width="100%",
                    ),
                    rx.hstack(
                        rx.button(
                            NovelState.startup_test_button_text,
                            on_click=NovelState.test_startup_config,
                            disabled=NovelState.startup_test_button_disabled,
                            background=rx.cond(
                                NovelState.startup_test_button_disabled,
                                "rgba(51, 65, 85, 0.82)",
                                "linear-gradient(135deg, #0f172a 0%, #0f766e 100%)",
                            ),
                            color="white",
                            border_radius="12px",
                            min_width="9rem",
                        ),
                        rx.button(
                            NovelState.startup_begin_button_text,
                            on_click=NovelState.apply_startup_config,
                            disabled=NovelState.startup_begin_button_disabled,
                            background=rx.cond(
                                NovelState.startup_begin_button_disabled,
                                "rgba(71, 85, 105, 0.82)",
                                "linear-gradient(135deg, #2563eb 0%, #06b6d4 100%)",
                            ),
                            color="white",
                            border_radius="12px",
                            min_width="9rem",
                        ),
                        width="100%",
                        spacing="3",
                        justify="end",
                    ),
                    spacing="3",
                    align="stretch",
                    width="100%",
                ),
                padding="1.15rem",
                width="100%",
            ),
            width="100%",
            spacing="4",
            align="stretch",
            max_width="1280px",
            margin="0 auto",
            padding="1.2rem 0 2rem",
        ),
        width="100%",
        min_height="100vh",
        padding="1.2rem",
        background=(
            "radial-gradient(circle at top left, rgba(14, 165, 233, 0.16) 0%, rgba(14, 165, 233, 0.0) 30%), "
            "radial-gradient(circle at top right, rgba(34, 197, 94, 0.12) 0%, rgba(34, 197, 94, 0.0) 28%), "
            "linear-gradient(180deg, #020617 0%, #0f172a 100%)"
        ),
    )
