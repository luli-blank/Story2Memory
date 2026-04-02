from __future__ import annotations

import reflex as rx

from ..state import Book, NovelState
from .common import glass_panel


def delete_book_dialog() -> rx.Component:
    return rx.cond(
        NovelState.delete_book_dialog_open,
        rx.box(
            rx.vstack(
                rx.text("确认删除", color="#f8fafc", font_size="1.05rem", font_weight="700"),
                rx.vstack(
                    rx.text(
                        "是否确认删除书籍信息、分析及相关对话？",
                        color="#cbd5e1",
                        font_size="0.9rem",
                        line_height="1.7",
                    ),
                    rx.text(
                        NovelState.delete_book_target_title,
                        color="#f8fafc",
                        font_size="0.92rem",
                        font_weight="600",
                    ),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                rx.hstack(
                    rx.button(
                        "取消",
                        on_click=NovelState.cancel_delete_book,
                        background="rgba(51, 65, 85, 0.88)",
                        color="#e2e8f0",
                        border_radius="10px",
                        min_width="96px",
                    ),
                    rx.button(
                        rx.cond(NovelState.is_deleting_book, "删除中...", "确定"),
                        on_click=NovelState.confirm_delete_book,
                        disabled=NovelState.is_deleting_book,
                        background="linear-gradient(135deg, #ef4444 0%, #f97316 100%)",
                        color="white",
                        border_radius="10px",
                        min_width="96px",
                    ),
                    justify="end",
                    spacing="3",
                    width="100%",
                ),
                padding="1.25rem",
                border_radius="18px",
                background="rgba(15, 23, 42, 0.96)",
                border="1px solid rgba(248, 113, 113, 0.35)",
                box_shadow="0 20px 60px rgba(2, 6, 23, 0.48)",
                spacing="4",
                width="min(92vw, 440px)",
            ),
            position="fixed",
            inset="0",
            background="rgba(2, 6, 23, 0.58)",
            backdrop_filter="blur(3px)",
            display="flex",
            align_items="center",
            justify_content="center",
            z_index="9998",
            padding="1.5rem",
        ),
        rx.box(),
    )


def book_card(book: Book) -> rx.Component:
    return rx.vstack(
        rx.box(
            rx.image(
                src=book.cover,
                width="100%",
                height="180px",
                object_fit="cover",
                border_radius="10px",
                border="1px solid rgba(148, 163, 184, 0.2)",
                on_click=NovelState.open_book(book.id, book.title),
                cursor="pointer",
                _hover={"opacity": 0.9},
            ),
            rx.button(
                "×",
                on_click=NovelState.prompt_delete_book(book.id, book.title),
                position="absolute",
                top="10px",
                right="10px",
                width="30px",
                height="30px",
                min_width="30px",
                padding="0",
                border_radius="999px",
                background="rgba(15, 23, 42, 0.82)",
                border="1px solid rgba(248, 113, 113, 0.4)",
                color="#fecaca",
                font_size="1rem",
                line_height="1",
                opacity="0",
                pointer_events="none",
                transform="scale(0.92)",
                transition="all 0.18s ease",
                _hover={"background": "rgba(127, 29, 29, 0.92)", "color": "white"},
                class_name="book-delete-button",
            ),
            position="relative",
            width="100%",
            _hover={
                "& .book-delete-button": {
                    "opacity": "1",
                    "pointerEvents": "auto",
                    "transform": "scale(1)",
                }
            },
        ),
        rx.text(
            book.title,
            font_weight="600",
            font_size="0.95rem",
            color="#e2e8f0",
            text_align="center",
            overflow="hidden",
            white_space="nowrap",
            text_overflow="ellipsis",
            width="100%",
        ),
        rx.text(
            book.meta,
            color="#94a3b8",
            font_size="0.75rem",
            text_align="center",
        ),
        spacing="1",
        padding="0.6rem",
        border_radius="12px",
        background="rgba(30, 41, 59, 0.45)",
        border="1px solid rgba(148, 163, 184, 0.12)",
        _hover={"transform": "translateY(-4px)", "transition": "all 0.3s ease", "border_color": "rgba(59, 130, 246, 0.5)"},
        width="100%",
    )


def selected_upload_file_item(file_name: str) -> rx.Component:
    return rx.text(
        file_name,
        color="#bfdbfe",
        font_size="0.8rem",
        overflow="hidden",
        white_space="nowrap",
        text_overflow="ellipsis",
        width="100%",
    )


def bookshelf_view() -> rx.Component:
    return rx.vstack(
        glass_panel(
            rx.vstack(
                rx.text("📚 我的书架", font_size="1.1rem", font_weight="700", color="#e2e8f0", text_align="center", width="100%"),
                rx.text(
                    "点击封面进入详情模式，开始深度分析。",
                    color="#94a3b8",
                    font_size="0.85rem",
                    text_align="center",
                    width="100%",
                ),
                spacing="1",
                align="center",
                width="100%",
            ),
        ),
        # 修改点：确保 grid 拥有 5 列，并设置宽度 100%
        rx.grid(
            rx.foreach(NovelState.bookshelf_items, book_card),
            columns="5",
            spacing="4",
            width="100%",
        ),
        glass_panel(
            rx.text("⬆️ 上传新书", font_size="1.05rem", font_weight="700", color="#e2e8f0"),
            rx.text("当前支持 txt / epub，上传成功后会立即刷新书架。", color="#cbd5e1", font_size="0.9rem"),
            rx.upload(
                rx.vstack(
                    rx.text("拖拽文件到这里，或点击选择", color="#93c5fd", font_weight="600"),
                    spacing="1",
                ),
                id="novel_upload",
                border="2px dashed rgba(56, 189, 248, 0.45)",
                border_radius="12px",
                width="100%",
                padding="1.2rem",
                background="rgba(15, 23, 42, 0.45)",
            ),
            rx.cond(
                rx.selected_files("novel_upload"),
                rx.vstack(
                    rx.text("已选择文件：", color="#cbd5e1", font_size="0.8rem"),
                    rx.foreach(rx.selected_files("novel_upload"), selected_upload_file_item),
                    spacing="1",
                    width="100%",
                ),
                rx.box(),
            ),
            rx.button(
                NovelState.upload_button_text,
                on_click=NovelState.handle_upload(rx.upload_files(upload_id="novel_upload")),
                disabled=NovelState.upload_button_disabled,
                background=rx.cond(
                    NovelState.upload_button_disabled,
                    "rgba(100, 116, 139, 0.8)",
                    "linear-gradient(135deg, #0ea5e9 0%, #22d3ee 100%)",
                ),
                color="white",
                border_radius="10px",
                width="100%",
            ),
            rx.cond(
                NovelState.upload_feedback,
                rx.text(
                    NovelState.upload_feedback,
                    color=rx.cond(NovelState.upload_feedback_is_error, "#fda4af", "#86efac"),
                    font_size="0.82rem",
                ),
                rx.box(),
            ),
            spacing="3",
            width="100%",
        ),
        delete_book_dialog(),
        width="100%",
        spacing="4",
        align="stretch",
    )
