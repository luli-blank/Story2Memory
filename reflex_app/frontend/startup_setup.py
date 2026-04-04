from __future__ import annotations

import reflex as rx

from ..state import NovelState
from .common import glass_panel


def _config_input(label: str, value, on_change, placeholder: str, *, input_type: str = "text") -> rx.Component:
    return rx.vstack(
        rx.text(label, color="#e2e8f0", font_weight="600", font_size="0.9rem"),
        rx.input(
            value=value,
            on_change=on_change,
            placeholder=placeholder,
            type=input_type,
            border_radius="12px",
            background="rgba(15, 23, 42, 0.72)",
            border="1px solid rgba(148, 163, 184, 0.24)",
            color="white",
            width="100%",
        ),
        align="start",
        spacing="1",
        width="100%",
    )


def _capability_header(title: str, checked, on_change, description: str) -> rx.Component:
    return rx.hstack(
        rx.vstack(
            rx.text(title, color="#f8fafc", font_weight="700", font_size="1rem"),
            rx.text(description, color="#94a3b8", font_size="0.8rem", line_height="1.6"),
            align="start",
            spacing="1",
        ),
        rx.spacer(),
        rx.switch(
            checked=checked,
            on_change=on_change,
            color_scheme="cyan",
        ),
        width="100%",
        align="center",
    )


def startup_setup_view() -> rx.Component:
    return rx.vstack(
        glass_panel(
            rx.vstack(
                rx.hstack(
                    rx.vstack(
                        rx.text("启动配置向导", color="#f8fafc", font_size="1.45rem", font_weight="800"),
                        rx.text(
                            "先确认模型与检索能力，再进入书架。配置会持久化到运行时配置文件，应用并重启后生效。",
                            color="#94a3b8",
                            font_size="0.88rem",
                            line_height="1.7",
                        ),
                        align="start",
                        spacing="1",
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
                ),
                rx.box(
                    rx.text(
                        NovelState.startup_runtime_path_label,
                        color=NovelState.startup_status_accent,
                        font_size="0.8rem",
                    ),
                    padding="0.45rem 0.7rem",
                    border_radius="999px",
                    background="rgba(8, 47, 73, 0.34)",
                    border="1px solid rgba(103, 232, 249, 0.16)",
                ),
                rx.cond(
                    NovelState.startup_validation_errors,
                    rx.vstack(
                        rx.text("当前仍缺少这些配置：", color="#fecdd3", font_size="0.84rem", font_weight="700"),
                        rx.foreach(
                            NovelState.startup_validation_errors,
                            lambda item: rx.text(f"• {item}", color="#fda4af", font_size="0.8rem"),
                        ),
                        align="start",
                        spacing="1",
                        width="100%",
                    ),
                    rx.text("基础配置完整后即可应用并重启；也可以直接进入书架。", color="#86efac", font_size="0.82rem"),
                ),
                rx.cond(
                    NovelState.startup_feedback,
                    rx.text(
                        NovelState.startup_feedback,
                        color=rx.cond(NovelState.startup_feedback_is_error, "#fda4af", "#86efac"),
                        font_size="0.82rem",
                        white_space="pre-wrap",
                    ),
                    rx.box(),
                ),
                rx.cond(
                    NovelState.startup_last_saved_at,
                    rx.text(
                        f"最近保存：{NovelState.startup_last_saved_at}",
                        color="#64748b",
                        font_size="0.76rem",
                    ),
                    rx.box(),
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
        ),
        glass_panel(
            rx.vstack(
                rx.text("基础 LLM", color="#f8fafc", font_size="1.05rem", font_weight="700"),
                rx.grid(
                    _config_input(
                        "LLM API Key",
                        NovelState.setup_llm_api_key,
                        NovelState.set_setup_llm_api_key,
                        "sk-...",
                        input_type="password",
                    ),
                    _config_input(
                        "LLM Base URL",
                        NovelState.setup_llm_base_url,
                        NovelState.set_setup_llm_base_url,
                        "https://api.example.com/v1",
                    ),
                    _config_input(
                        "LLM Model",
                        NovelState.setup_llm_model,
                        NovelState.set_setup_llm_model,
                        "gpt-4.1 / deepseek / qwen ...",
                    ),
                    columns="3",
                    spacing="4",
                    width="100%",
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
        ),
        glass_panel(
            rx.vstack(
                _capability_header(
                    "向量召回",
                    NovelState.setup_vector_retrieval_enabled,
                    NovelState.set_setup_vector_retrieval_enabled,
                    "启用后使用 Embedding + Qdrant 的向量召回路径，不启用时保留基础 LLM 能力。",
                ),
                rx.cond(
                    NovelState.setup_vector_retrieval_enabled,
                    rx.grid(
                        _config_input(
                            "Embedding API Key",
                            NovelState.setup_embed_api_key,
                            NovelState.set_setup_embed_api_key,
                            "embed-key",
                            input_type="password",
                        ),
                        _config_input(
                            "Embedding Base URL",
                            NovelState.setup_embed_base_url,
                            NovelState.set_setup_embed_base_url,
                            "https://embed.example.com/v1",
                        ),
                        _config_input(
                            "Embedding Model",
                            NovelState.setup_embed_model,
                            NovelState.set_setup_embed_model,
                            "text-embedding-3-large",
                        ),
                        columns="3",
                        spacing="4",
                        width="100%",
                    ),
                    rx.box(),
                ),
                rx.cond(
                    NovelState.setup_vector_retrieval_enabled,
                    _config_input(
                        "Qdrant URL",
                        NovelState.setup_qdrant_url,
                        NovelState.set_setup_qdrant_url,
                        "http://qdrant:6333",
                    ),
                    rx.box(),
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
        ),
        glass_panel(
            rx.vstack(
                _capability_header(
                    "Rerank",
                    NovelState.setup_rerank_enabled,
                    NovelState.set_setup_rerank_enabled,
                    "启用后在召回结果上追加 rerank 排序；关闭时不会调用 rerank 客户端。",
                ),
                rx.cond(
                    NovelState.setup_rerank_enabled,
                    rx.vstack(
                        rx.vstack(
                            rx.text("Rerank Provider", color="#e2e8f0", font_weight="600", font_size="0.9rem"),
                            rx.select(
                                ["local", "openai_compatible", "qwen", "ark"],
                                value=NovelState.setup_rerank_provider,
                                on_change=NovelState.set_setup_rerank_provider,
                                width="100%",
                            ),
                            align="start",
                            spacing="1",
                            width="100%",
                        ),
                        rx.grid(
                            _config_input(
                                "Rerank Base URL",
                                NovelState.setup_rerank_base_url,
                                NovelState.set_setup_rerank_base_url,
                                "http://rerank-local:8000/rerank",
                            ),
                            _config_input(
                                "Rerank API Key",
                                NovelState.setup_rerank_api_key,
                                NovelState.set_setup_rerank_api_key,
                                "local provider 可留空",
                                input_type="password",
                            ),
                            _config_input(
                                "Rerank Model",
                                NovelState.setup_rerank_model,
                                NovelState.set_setup_rerank_model,
                                "BAAI/bge-reranker-v2-m3",
                            ),
                            columns="3",
                            spacing="4",
                            width="100%",
                        ),
                        spacing="3",
                        align="start",
                        width="100%",
                    ),
                    rx.box(),
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
        ),
        glass_panel(
            rx.vstack(
                _capability_header(
                    "启动期预热",
                    NovelState.setup_prewarm_enabled,
                    NovelState.set_setup_prewarm_enabled,
                    "启用后会在应用启动阶段预热检索运行时；默认关闭以降低首启负担。",
                ),
                rx.hstack(
                    rx.button(
                        NovelState.startup_save_button_text,
                        on_click=NovelState.save_startup_config,
                        background="rgba(30, 41, 59, 0.88)",
                        color="#e2e8f0",
                        border_radius="12px",
                    ),
                    rx.button(
                        "应用并重启",
                        on_click=NovelState.apply_startup_config,
                        disabled=NovelState.startup_apply_button_disabled,
                        background=rx.cond(
                            NovelState.startup_apply_button_disabled,
                            "rgba(100, 116, 139, 0.82)",
                            "linear-gradient(135deg, #0891b2 0%, #22c55e 100%)",
                        ),
                        color="white",
                        border_radius="12px",
                    ),
                    rx.spacer(),
                    rx.button(
                        "进入书架",
                        on_click=NovelState.enter_bookshelf,
                        background="linear-gradient(135deg, #2563eb 0%, #0ea5e9 100%)",
                        color="white",
                        border_radius="12px",
                    ),
                    width="100%",
                    align="center",
                    spacing="3",
                ),
                spacing="3",
                align="start",
                width="100%",
            ),
        ),
        width="100%",
        spacing="4",
        align="stretch",
    )
