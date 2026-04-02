from __future__ import annotations

import reflex as rx

from ..state import NovelState
from .common import glass_panel


def detail_view() -> rx.Component:
    return rx.vstack(
        glass_panel(
            rx.hstack(
                rx.text("📖", font_size="1.6rem"),
                rx.vstack(
                    rx.text(NovelState.context_label, font_size="1.2rem", font_weight="700", color="#e2e8f0"),
                    rx.text("书籍详情/阅读区域（可扩展章节目录、摘要、图谱）", color="#cbd5e1", font_size="0.9rem"),
                    align="start",
                    spacing="1",
                ),
                align="center",
                spacing="3",
            )
        ),
        glass_panel(
            rx.hstack(
                rx.image(
                    src=NovelState.current_book_cover,
                    width="170px",
                    height="230px",
                    object_fit="cover",
                    border_radius="12px",
                ),
                rx.vstack(
                    rx.text(NovelState.current_book_status_text, color="#dbeafe"),
                    rx.text("阅读进度：0%（示例）", color="#dbeafe"),
                    rx.text("最近操作：可在右侧向 Agent 提问人物、情节、关系分析。", color="#dbeafe"),
                    rx.hstack(
                        rx.button(
                            "← 返回书架",
                            on_click=NovelState.back_to_bookshelf,
                            variant="outline",
                            border="1px solid rgba(59, 130, 246, 0.8)",
                            color="#bfdbfe",
                            background="rgba(30, 41, 59, 0.35)",
                            border_radius="10px",
                        ),
                        rx.button(
                            NovelState.analyze_button_text,
                            on_click=NovelState.start_chapter_analysis,
                            disabled=NovelState.analyze_button_disabled,
                            background="linear-gradient(135deg, #0ea5e9 0%, #10b981 100%)",
                            color="white",
                            border_radius="10px",
                        ),
                        rx.button(
                            "角色档案",
                            on_click=NovelState.open_character_archive,
                            variant="outline",
                            border="1px solid rgba(34, 211, 238, 0.8)",
                            color="#a5f3fc",
                            background="rgba(8, 47, 73, 0.35)",
                            border_radius="10px",
                        ),
                        rx.button(
                            "关系图",
                            on_click=NovelState.open_relation_graph,
                            variant="outline",
                            border="1px solid rgba(125, 211, 252, 0.55)",
                            color="#E0F2FE",
                            background="rgba(15, 23, 42, 0.35)",
                            border_radius="10px",
                        ),
                        spacing="3",
                        margin_top="0.6rem",
                    ),
                    align="start",
                    spacing="2",
                    width="100%",
                ),
                align="start",
                spacing="5",
                width="100%",
            )
        ),
        width="100%",
        spacing="4",
        align="stretch",
    )
