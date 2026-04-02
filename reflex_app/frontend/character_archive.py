from __future__ import annotations

import reflex as rx

from ..state import CharacterArchiveCard, NovelState
from .common import archive_section_heading, archive_section_shell, glass_panel


def archive_alias_preview(item: CharacterArchiveCard) -> rx.Component:
    return rx.cond(
        item.alias_preview_text,
        rx.box(
            rx.text(
                item.alias_preview_text,
                color="#c6f1ff",
                font_size="0.76rem",
                line_height="1.45",
                width="100%",
            ),
            padding="0.45rem 0.6rem",
            border="1px solid rgba(125, 211, 252, 0.12)",
            border_radius="12px",
            background="rgba(12, 22, 43, 0.72)",
            width="100%",
        ),
        rx.text("暂无别名预览", color="#64748b", font_size="0.78rem"),
    )


def character_archive_card(item: CharacterArchiveCard) -> rx.Component:
    return archive_section_shell(
        rx.vstack(
            rx.hstack(
                rx.box(
                    rx.text("角色档案", color="#67e8f9", font_size="0.68rem", font_weight="700", text_transform="uppercase"),
                    padding="0.28rem 0.52rem",
                    border_radius="999px",
                    background="rgba(8, 47, 73, 0.46)",
                    border="1px solid rgba(103, 232, 249, 0.14)",
                ),
                rx.spacer(),
                rx.cond(
                    item.first_chapter_index > 0,
                    rx.text(
                        f"Chapter {item.first_chapter_index} - {item.last_chapter_index}",
                        color="#8197b5",
                        font_size="0.74rem",
                    ),
                    rx.text("暂无章节范围", color="#8197b5", font_size="0.74rem"),
                ),
                width="100%",
                align="center",
            ),
            rx.hstack(
                rx.vstack(
                    rx.text(
                        item.name,
                        color="#f8fafc",
                        font_size="1.08rem",
                        font_weight="700",
                        width="100%",
                        letter_spacing="0.01em",
                    ),
                    rx.text("已归档角色摘要入口", color="#64748b", font_size="0.74rem"),
                    align="start",
                    spacing="0",
                    width="100%",
                ),
                rx.box(
                    rx.text(item.record_count.to_string(), color="#f8fafc", font_size="1.05rem", font_weight="800"),
                    rx.text("记录", color="#7dd3fc", font_size="0.66rem"),
                    padding="0.55rem 0.7rem",
                    border_radius="16px",
                    background="linear-gradient(165deg, rgba(6, 182, 212, 0.22) 0%, rgba(37, 99, 235, 0.12) 100%)",
                    border="1px solid rgba(125, 211, 252, 0.14)",
                    min_width="78px",
                    text_align="center",
                ),
                width="100%",
                align="start",
                spacing="3",
            ),
            archive_alias_preview(item),
            rx.button(
                "查看角色",
                on_click=NovelState.open_character_detail(item.id),
                width="100%",
                background="linear-gradient(135deg, #1d4ed8 0%, #0f766e 100%)",
                color="white",
                border_radius="12px",
                font_weight="700",
                box_shadow="0 12px 24px rgba(8, 47, 73, 0.32)",
                _hover={"transform": "translateY(-1px)", "opacity": 0.95},
            ),
            spacing="2",
            width="100%",
            align="stretch",
        ),
        min_height="206px",
        _hover={
            "transform": "translateY(-2px)",
            "border_color": "rgba(125, 211, 252, 0.2)",
            "transition": "all 0.2s ease",
        },
    )


def character_archive_view() -> rx.Component:
    return rx.vstack(
        archive_section_shell(
            rx.hstack(
                archive_section_heading(
                    "角色档案",
                    "按记录数量排序浏览角色档案。卡片采用分页展示，进入单角色页可查看完整画像与关系。",
                    "Dossier Index",
                ),
                rx.spacer(),
                rx.button(
                    "← 返回书籍详情",
                    on_click=NovelState.back_to_book_detail,
                    variant="outline",
                    border="1px solid rgba(96, 165, 250, 0.42)",
                    color="#bfdbfe",
                    background="rgba(15, 23, 42, 0.55)",
                    border_radius="12px",
                ),
                width="100%",
                align="center",
            ),
            background="linear-gradient(145deg, rgba(15, 23, 42, 0.96) 0%, rgba(7, 18, 36, 0.98) 100%)",
        ),
        rx.cond(
            NovelState.character_archive_loading,
            archive_section_shell(
                rx.text("正在加载角色档案列表...", color="#93c5fd", font_size="0.92rem"),
                width="100%",
            ),
            rx.cond(
                NovelState.character_archive_items,
                rx.vstack(
                    rx.grid(
                        rx.foreach(NovelState.character_archive_page_items, character_archive_card),
                        columns="2",
                        spacing="4",
                        width="100%",
                    ),
                    rx.cond(
                        NovelState.character_archive_total_pages > 1,
                        glass_panel(
                            rx.hstack(
                                rx.button(
                                    "上一页",
                                    on_click=NovelState.character_archive_prev_page,
                                    disabled=NovelState.character_archive_prev_disabled,
                                    variant="outline",
                                    border="1px solid rgba(96, 165, 250, 0.32)",
                                    color="#bfdbfe",
                                    background="rgba(15, 23, 42, 0.55)",
                                    border_radius="12px",
                                ),
                                rx.spacer(),
                                rx.box(
                                    rx.text(
                                        NovelState.character_archive_page_label,
                                        color="#dbeafe",
                                        font_size="0.88rem",
                                        font_weight="700",
                                    ),
                                    padding="0.5rem 0.82rem",
                                    border_radius="999px",
                                    background="rgba(15, 23, 42, 0.72)",
                                    border="1px solid rgba(148, 163, 184, 0.14)",
                                ),
                                rx.spacer(),
                                rx.button(
                                    "下一页",
                                    on_click=NovelState.character_archive_next_page,
                                    disabled=NovelState.character_archive_next_disabled,
                                    variant="outline",
                                    border="1px solid rgba(103, 232, 249, 0.32)",
                                    color="#a5f3fc",
                                    background="rgba(8, 47, 73, 0.46)",
                                    border_radius="12px",
                                ),
                                width="100%",
                                align="center",
                            ),
                            width="100%",
                            background="linear-gradient(145deg, rgba(10, 18, 34, 0.95) 0%, rgba(6, 14, 28, 0.95) 100%)",
                        ),
                        rx.box(),
                    ),
                    width="100%",
                    spacing="4",
                    align="stretch",
                ),
                archive_section_shell(
                    rx.text("当前书籍暂无可展示的角色档案数据。", color="#94a3b8", font_size="0.92rem"),
                    width="100%",
                ),
            ),
        ),
        width="100%",
        spacing="4",
        align="stretch",
    )
