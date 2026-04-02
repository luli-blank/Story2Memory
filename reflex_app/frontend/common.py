from __future__ import annotations

import reflex as rx


def glass_panel(*children, **props) -> rx.Component:
    merged_props = {
        "border": "1px solid rgba(148, 163, 184, 0.25)",
        "background": "rgba(15, 23, 42, 0.65)",
        "backdrop_filter": "blur(8px)",
        "border_radius": "18px",
        "box_shadow": "0 12px 30px rgba(15, 23, 42, 0.35)",
        "padding": "1rem",
    }
    merged_props.update(props)
    return rx.box(*children, **merged_props)


def archive_section_shell(*children, **props) -> rx.Component:
    merged_props = {
        "border": "1px solid rgba(148, 163, 184, 0.18)",
        "background": "linear-gradient(160deg, rgba(15, 23, 42, 0.94) 0%, rgba(8, 15, 30, 0.92) 100%)",
        "border_radius": "22px",
        "box_shadow": "0 22px 50px rgba(2, 6, 23, 0.42)",
        "padding": "1.15rem 1.2rem",
        "position": "relative",
        "overflow": "hidden",
    }
    merged_props.update(props)
    return rx.box(
        rx.box(
            position="absolute",
            top="0",
            left="0",
            right="0",
            height="1px",
            background="linear-gradient(90deg, rgba(103, 232, 249, 0.0) 0%, rgba(103, 232, 249, 0.65) 48%, rgba(103, 232, 249, 0.0) 100%)",
        ),
        *children,
        **merged_props,
    )


def archive_stat_chip(label: str, value: str, accent: str = "#67e8f9") -> rx.Component:
    return rx.vstack(
        rx.text(
            label,
            color="#7f93ad",
            font_size="0.68rem",
            text_transform="uppercase",
            letter_spacing="0.12em",
        ),
        rx.text(
            value,
            color="#f8fafc",
            font_size="1rem",
            font_weight="800",
        ),
        padding="0.72rem 0.88rem",
        border_radius="16px",
        background="linear-gradient(165deg, rgba(10, 20, 38, 0.9) 0%, rgba(7, 15, 29, 0.84) 100%)",
        border=f"1px solid {accent}22",
        box_shadow="inset 0 1px 0 rgba(255,255,255,0.03)",
        align="start",
        spacing="0",
        min_width="128px",
    )


def archive_section_heading(title: str, subtitle: str = "", tag: str = "") -> rx.Component:
    return rx.hstack(
        rx.vstack(
            rx.cond(
                tag,
                rx.box(
                    rx.text(
                        tag,
                        color="#67e8f9",
                        font_size="0.66rem",
                        font_weight="800",
                        letter_spacing="0.14em",
                    ),
                    padding="0.22rem 0.52rem",
                    border_radius="999px",
                    background="rgba(8, 47, 73, 0.42)",
                    border="1px solid rgba(103, 232, 249, 0.14)",
                ),
                rx.box(),
            ),
            rx.text(
                title,
                color="#f8fafc",
                font_size="1.28rem",
                font_weight="800",
                letter_spacing="0.01em",
            ),
            rx.cond(
                subtitle,
                rx.text(
                    subtitle,
                    color="#8ea6c3",
                    font_size="0.86rem",
                    line_height="1.6",
                ),
                rx.box(),
            ),
            align="start",
            spacing="1",
        ),
        width="100%",
        align="start",
    )


def collapsible_glass_panel(title: str, body: rx.Component, *, value: str, **props) -> rx.Component:
    merged_props = {
        "width": "100%",
        "padding": "0.95rem 1.05rem",
    }
    merged_props.update(props)
    return archive_section_shell(
        rx.accordion.root(
            rx.accordion.item(
                rx.accordion.header(
                    rx.accordion.trigger(
                        rx.hstack(
                            rx.vstack(
                                rx.text(title, color="#f8fafc", font_weight="700", font_size="1rem", letter_spacing="0.01em"),
                                rx.text("点击展开查看详细内容", color="#64748b", font_size="0.72rem"),
                                align="start",
                                spacing="0",
                            ),
                            rx.spacer(),
                            rx.box(
                                rx.text("展开", color="#9bdaf1", font_size="0.72rem", font_weight="600"),
                                padding="0.28rem 0.55rem",
                                border="1px solid rgba(103, 232, 249, 0.18)",
                                border_radius="999px",
                                background="rgba(8, 47, 73, 0.38)",
                            ),
                            width="100%",
                            align="center",
                        ),
                    )
                ),
                rx.accordion.content(
                    rx.box(
                        body,
                        padding_top="0.85rem",
                        width="100%",
                    )
                ),
                value=value,
                width="100%",
            ),
            type="multiple",
            collapsible=True,
            width="100%",
        ),
        **merged_props,
    )
