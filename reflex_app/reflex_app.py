from __future__ import annotations

from pathlib import Path

import reflex as rx
from starlette.staticfiles import StaticFiles

from .frontend.bookshelf import bookshelf_view
from .frontend.character_archive import character_archive_view
from .frontend.character_detail import character_detail_view
from .frontend.common import glass_panel
from .frontend.detail import detail_view
from .frontend.relation_graph import relation_graph_view
from .state import ConfirmedEvidence, ChatMessage, NovelState

ROOT_DIR = Path(__file__).resolve().parents[1]
PICTURE_DIR = ROOT_DIR / "data" / "picture"


def chat_bubble(message: ChatMessage) -> rx.Component:
    is_user = message.role == "user"
    return rx.box(
        rx.text(message.content, white_space="pre-wrap", line_height="1.5"),
        align_self=rx.cond(is_user, "flex-end", "flex-start"),
        max_width="90%",
        padding="0.68rem 0.82rem",
        border_radius=rx.cond(is_user, "16px 16px 6px 16px", "16px 16px 16px 6px"),
        border="1px solid rgba(148, 163, 184, 0.25)",
        background=rx.cond(is_user, "linear-gradient(135deg, #2563eb 0%, #0891b2 100%)", "rgba(30, 41, 59, 0.85)"),
        color="white",
    )


def confirmed_evidence_item(item: ConfirmedEvidence) -> rx.Component:
    chapter_label = f"Chapter {item.chapter_index}"
    return rx.accordion.item(
        rx.accordion.header(
            rx.accordion.trigger(
                rx.vstack(
                    rx.text(
                        item.claim,
                        color="#e2e8f0",
                        font_weight="700",
                        font_size="0.88rem",
                        line_height="1.45",
                        white_space="pre-wrap",
                        width="100%",
                    ),
                    rx.text(
                        f"{chapter_label} · {item.label}",
                        color="#67e8f9",
                        font_size="0.76rem",
                    ),
                    rx.cond(
                        item.source_name,
                        rx.text(item.source_name, color="#94a3b8", font_size="0.76rem"),
                        rx.text("已确认原文证据", color="#94a3b8", font_size="0.76rem"),
                    ),
                    rx.text(f"可信度 {item.confidence:.2f}", color="#7dd3fc", font_size="0.72rem"),
                    align="start",
                    spacing="1",
                    width="100%",
                ),
                width="100%",
            )
        ),
        rx.accordion.content(
            rx.box(
                rx.box(
                    rx.html(item.highlighted_html),
                    color="#e2e8f0",
                    font_size="0.84rem",
                    line_height="1.68",
                    white_space="normal",
                    background="linear-gradient(180deg, rgba(2, 6, 23, 0.94) 0%, rgba(15, 23, 42, 0.96) 100%)",
                    border="1px solid rgba(125, 211, 252, 0.16)",
                    border_radius="14px",
                    padding="1rem 1.05rem",
                    width="100%",
                    max_width="100%",
                    box_shadow="0 18px 40px rgba(2, 6, 23, 0.42)",
                ),
                width="100%",
                overflow_x="hidden",
            )
        ),
        value=f"{item.subquery_id}-{item.chapter_index}",
        border="1px solid rgba(148, 163, 184, 0.18)",
        border_radius="12px",
        background="rgba(15, 23, 42, 0.45)",
        margin_bottom="0.55rem",
        overflow="hidden",
        width="100%",
    )


def confirmed_evidence_panel() -> rx.Component:
    return glass_panel(
        rx.vstack(
            rx.text("evidence", font_size="1.02rem", font_weight="700", color="#e2e8f0"),
            rx.text("标题是最终采用的精简证据；展开后查看对应 chapter 内容，高亮部分为最直接的支撑片段。", color="#94a3b8", font_size="0.82rem"),
            rx.cond(
                NovelState.confirmed_evidence,
                rx.box(
                    rx.accordion.root(
                        rx.foreach(NovelState.confirmed_evidence, confirmed_evidence_item),
                        type="multiple",
                        collapsible=True,
                        width="100%",
                    ),
                    width="100%",
                    max_height="30vh",
                    overflow_y="auto",
                    overflow_x="hidden",
                ),
                rx.text("当前暂无可展示的已确认章节证据。", color="#64748b", font_size="0.82rem"),
            ),
            width="100%",
            align="stretch",
            spacing="2",
        ),
        width="100%",
    )


def chat_panel() -> rx.Component:
    return glass_panel(
        rx.vstack(
            rx.text(NovelState.chat_title, font_size="1.05rem", font_weight="700", color="#e2e8f0"),
            rx.hstack(
                rx.text("当前上下文：", color="#cbd5e1", font_size="0.9rem"),
                rx.text(NovelState.context_label, color="#cbd5e1", font_size="0.9rem"),
                spacing="1",
            ),
            rx.box(
                rx.vstack(
                    rx.foreach(NovelState.chat_messages, chat_bubble),
                    width="100%",
                    align="stretch",
                    spacing="3",
                ),
                width="100%",
                height="62vh",
                overflow_y="auto",
                padding_right="0.25rem",
            ),
            rx.cond(
                NovelState.is_generating,
                rx.text("Agent 正在思考，请稍候...", color="#93c5fd", font_size="0.82rem"),
                rx.box(),
            ),
            rx.hstack(
                rx.input(
                    value=NovelState.chat_input,
                    on_change=NovelState.set_chat_input,
                    placeholder=rx.cond(
                        NovelState.chat_mode == "roleplay",
                        "以角色身份对话...",
                        "输入问题...",
                    ),
                    disabled=NovelState.is_generating,
                    border_radius="10px",
                    background="rgba(15, 23, 42, 0.65)",
                    color="white",
                ),
                rx.button(
                    rx.cond(NovelState.is_generating, "思考中...", "发送"),
                    on_click=NovelState.send_message,
                    disabled=NovelState.is_generating,
                    background="linear-gradient(135deg, #2563eb 0%, #06b6d4 100%)",
                    color="white",
                    border_radius="10px",
                ),
                width="100%",
                spacing="2",
            ),
            width="100%",
            align="stretch",
            spacing="3",
        ),
        position="sticky",
        top="0.8rem",
    )


def analysis_popup() -> rx.Component:
    return rx.cond(
        NovelState.analysis_feedback_visible,
        rx.box(
            rx.text(
                NovelState.analysis_feedback,
                color="white",
                font_weight="600",
                font_size="0.9rem",
                white_space="pre-wrap",
            ),
            position="fixed",
            top="1.2rem",
            left="1.2rem",
            max_width="340px",
            padding="0.9rem 1rem",
            border_radius="12px",
            border="1px solid rgba(148, 163, 184, 0.35)",
            background=rx.cond(
                NovelState.analysis_feedback_is_error,
                "linear-gradient(135deg, rgba(190, 24, 93, 0.95) 0%, rgba(157, 23, 77, 0.95) 100%)",
                "linear-gradient(135deg, rgba(14, 116, 144, 0.95) 0%, rgba(5, 150, 105, 0.95) 100%)",
            ),
            box_shadow="0 14px 30px rgba(2, 6, 23, 0.5)",
            z_index="9999",
        ),
        rx.box(),
    )


def index() -> rx.Component:
    return rx.box(
        rx.cond(
            NovelState.page_mode == "relation_graph",
            rx.box(
                relation_graph_view(),
                max_width="1600px",
                margin="0 auto",
                padding="1.2rem",
                min_height="100vh",
            ),
            rx.box(
                rx.hstack(
                    rx.vstack(
                        rx.box(
                            rx.cond(
                                NovelState.page_mode == "detail",
                                detail_view(),
                                rx.cond(
                                    NovelState.page_mode == "character_archive",
                                    character_archive_view(),
                                    rx.cond(
                                        NovelState.page_mode == "character_detail",
                                        character_detail_view(),
                                        bookshelf_view(),
                                    ),
                                ),
                            ),
                            width="100%",
                        ),
                        rx.cond(NovelState.page_mode == "detail", confirmed_evidence_panel(), rx.box()),
                        flex="3",
                        width="75%",
                        align="stretch",
                        spacing="4",
                    ),
                    rx.box(
                        chat_panel(),
                        flex="1",
                        width="25%",
                    ),
                    align="start",
                    spacing="5",
                    width="100%",
                ),
                max_width="1600px",
                margin="0 auto",
                padding="1.2rem",
                min_height="100vh",
            ),
        ),
        analysis_popup(),
        background="radial-gradient(circle at 12% 8%, rgba(30, 64, 175, 0.28), transparent 44%), radial-gradient(circle at 85% 5%, rgba(6, 182, 212, 0.25), transparent 42%), #020617",
        min_height="100vh",
    )


app = rx.App(
    theme=rx.theme(
        appearance="dark",
        radius="large",
        accent_color="blue",
    )
)
PICTURE_DIR.mkdir(parents=True, exist_ok=True)
if not any(getattr(route, "path", None) == "/covers" for route in app._api.routes):
    app._api.mount("/covers", StaticFiles(directory=str(PICTURE_DIR)), name="covers")
app.add_page(index, title="Story2Memory · Reflex", on_load=NovelState.load_books)
