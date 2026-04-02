from __future__ import annotations

import reflex as rx

from ..state import (
    CharacterEmotionalRelationTimeline,
    CharacterEmotionalRelationView,
    CharacterProfileView,
    CharacterRelationHistory,
    CharacterRelationView,
    CharacterVolumeArc,
    NovelState,
)

BG_APP = "#0B1120"
PANEL_BG = "rgba(15, 23, 42, 0.45)"
ACCENT = "#38BDF8"
TEXT_PRIMARY = "#F8FAFC"
TEXT_SECONDARY = "#CBD5E1"
TEXT_TERTIARY = "#64748B"
EMPTY_TEXT = "#475569"
DIVIDER = "1px solid rgba(255,255,255,0.05)"
TRACK = "#1E293B"

SAFE_AREA_X = "clamp(2rem, 5vw, 6rem)"
INNER_PANEL_PADDING = "1.5rem 2rem"
DETAIL_PANE_HEIGHT = "calc(100vh - 300px)"
RELATION_HISTORY_HEIGHT = "calc(100vh - 520px)"
SCROLL_CLASS = "codex-detail-scroll"


def _scrollbar_style_tag() -> rx.Component:
    return rx.el.style(
        f"""
.{SCROLL_CLASS} {{
  scrollbar-width: thin;
  scrollbar-color: rgba(255,255,255,0.1) transparent;
}}
.{SCROLL_CLASS}::-webkit-scrollbar {{
  width: 4px;
  height: 4px;
}}
.{SCROLL_CLASS}::-webkit-scrollbar-track {{
  background: transparent;
}}
.{SCROLL_CLASS}::-webkit-scrollbar-thumb {{
  background: rgba(255,255,255,0.1);
  border-radius: 999px;
}}
.{SCROLL_CLASS}::-webkit-scrollbar-thumb:hover {{
  background: rgba(255,255,255,0.2);
}}
"""
    )


def _empty_state(text: str) -> rx.Component:
    return rx.text(text, color=EMPTY_TEXT, font_style="italic", font_size="0.9rem", font_weight="400")


def _join_items(items: list[str], sep: str = "，"):
    if hasattr(items, "join"):
        return items.join(sep)
    return sep.join([str(item or "").strip() for item in items or [] if str(item or "").strip()])


def _normalized_score(items: list[str]):
    count = items.length() if hasattr(items, "length") else len(items or [])
    raw_score = count * 20
    return rx.cond(raw_score > 100, 100, raw_score)


def _section_title(title: str) -> rx.Component:
    return rx.hstack(
        rx.box(width="3px", height="1.05rem", border_radius="999px", background=ACCENT),
        rx.text(title, color="#93C5FD", font_size="1.05rem", font_weight="700"),
        spacing="2",
        align="center",
        width="100%",
    )


def _profile_text_block(title: str, items: list[str], empty_text: str) -> rx.Component:
    return rx.box(
        _section_title(title),
        rx.cond(
            items,
            rx.text(_join_items(items), color=TEXT_SECONDARY, line_height="1.7", white_space="pre-wrap", font_weight="400"),
            _empty_state(empty_text),
        ),
        width="100%",
        padding_bottom="1rem",
        border_bottom=DIVIDER,
        margin_bottom="2.5rem",
    )


def _profile_summary_block(profile: CharacterProfileView) -> rx.Component:
    return rx.box(
        _section_title("身份概览"),
        rx.cond(
            profile.identity_summary,
            rx.text(profile.identity_summary, color=TEXT_SECONDARY, line_height="1.72", white_space="pre-wrap", font_weight="400"),
            _empty_state("No profile summary recorded."),
        ),
        rx.cond(
            profile.aliases,
            rx.hstack(
                rx.text("别名", color=TEXT_TERTIARY, font_size="0.84rem"),
                rx.text(_join_items(profile.aliases), color=ACCENT, font_size="0.88rem", font_style="italic"),
                spacing="2",
                align="center",
                width="100%",
            ),
            rx.box(),
        ),
        width="100%",
        padding_bottom="1rem",
        border_bottom=DIVIDER,
        margin_bottom="2.5rem",
        spacing="2",
    )


def _profile_radar_data(profile: CharacterProfileView) -> list[dict[str, object]]:
    return [
        {"metric": "叙事定位", "score": _normalized_score(profile.narrative_role)},
        {"metric": "能力资源", "score": _normalized_score(profile.abilities_and_resources)},
        {"metric": "目标动机", "score": _normalized_score(profile.goals_and_motivation)},
        {"metric": "当前状态", "score": _normalized_score(profile.current_state)},
        {"metric": "关键转折", "score": _normalized_score(profile.turning_points)},
        {"metric": "关键事件", "score": _normalized_score(profile.key_events)},
    ]


def _profile_tab_content(profile: CharacterProfileView) -> rx.Component:
    return rx.grid(
        rx.box(
            rx.box(
                rx.vstack(
                    rx.text("Profile Metrics", color=TEXT_TERTIARY, font_size="0.8rem", text_transform="uppercase", font_weight="600"),
                    rx.box(
                        rx.recharts.responsive_container(
                            rx.recharts.radar_chart(
                                rx.recharts.polar_grid(stroke="rgba(203,213,225,0.2)"),
                                rx.recharts.polar_angle_axis(data_key="metric", tick={"fill": "#94A3B8", "fontSize": 12}),
                                rx.recharts.polar_radius_axis(domain=[0, 100], tick=False, axis_line=False),
                                rx.recharts.radar(
                                    data_key="score",
                                    stroke=ACCENT,
                                    fill=ACCENT,
                                    fill_opacity=0.3,
                                    stroke_width=2,
                                ),
                                data=_profile_radar_data(profile),
                            ),
                            width="100%",
                            height=340,
                        ),
                        width="100%",
                    ),
                    spacing="3",
                    width="100%",
                    align="stretch",
                ),
                width="100%",
                height=DETAIL_PANE_HEIGHT,
                padding=INNER_PANEL_PADDING,
                background=PANEL_BG,
                border_radius="18px",
            ),
            width="100%",
            position="sticky",
            top="0.6rem",
            align_self="start",
        ),
        rx.box(
            rx.vstack(
                _profile_summary_block(profile),
                _profile_text_block("叙事定位", profile.narrative_role, "No narrative role recorded."),
                _profile_text_block("性格与风格", profile.personality_and_style, "No personality data recorded."),
                _profile_text_block("目标与动机", profile.goals_and_motivation, "No goals or motivation recorded."),
                _profile_text_block("立场与阵营", profile.stance_and_alignment, "No stance/alignment recorded."),
                _profile_text_block("能力与资源", profile.abilities_and_resources, "No abilities/resources recorded."),
                _profile_text_block("稳定画像", profile.stable_profile, "No stable profile recorded."),
                _profile_text_block("当前状态", profile.current_state, "No current state recorded."),
                _profile_text_block("关键转折", profile.turning_points, "No turning points recorded."),
                _profile_text_block("关键事件", profile.key_events, "No key events recorded."),
                spacing="0",
                width="100%",
                align="stretch",
            ),
            width="100%",
            max_height=DETAIL_PANE_HEIGHT,
            overflow_y="auto",
            class_name=SCROLL_CLASS,
            padding=INNER_PANEL_PADDING,
            background=PANEL_BG,
            border_radius="18px",
            padding_right="1.5rem",
        ),
        width="100%",
        grid_template_columns="4fr 6fr",
        gap="1.8rem",
    )


def _speech_tab_content(profile: CharacterProfileView) -> rx.Component:
    return rx.box(
        rx.vstack(
            rx.box(
                _section_title("语言风格总览"),
                rx.cond(
                    profile.style_summary,
                    rx.text(profile.style_summary, color=TEXT_SECONDARY, line_height="1.72", white_space="pre-wrap", font_weight="400"),
                    _empty_state("No style summary recorded."),
                ),
                width="100%",
                padding_bottom="1rem",
                border_bottom=DIVIDER,
                margin_bottom="2.5rem",
            ),
            _profile_text_block("语言风格", profile.speech_style, "No speech style recorded."),
            rx.box(
                _section_title("语言风格实例"),
                rx.cond(
                    profile.style_samples,
                    rx.vstack(
                        rx.foreach(
                            profile.style_samples,
                            lambda item: rx.box(
                                rx.text(item.scene, color=TEXT_TERTIARY, font_size="0.82rem", font_weight="600"),
                                rx.text("“" + item.quote + "”", color=TEXT_SECONDARY, line_height="1.7", white_space="pre-wrap", font_weight="400"),
                                width="100%",
                                padding_bottom="1rem",
                                border_bottom=DIVIDER,
                                margin_bottom="1rem",
                            ),
                        ),
                        spacing="0",
                        width="100%",
                        align="stretch",
                    ),
                    _empty_state("No style samples recorded."),
                ),
                width="100%",
                padding_bottom="1rem",
                border_bottom=DIVIDER,
                margin_bottom="2.5rem",
            ),
            spacing="0",
            width="100%",
            align="stretch",
        ),
        width="100%",
        max_height=DETAIL_PANE_HEIGHT,
        overflow_y="auto",
        class_name=SCROLL_CLASS,
        padding=INNER_PANEL_PADDING,
        background=PANEL_BG,
        border_radius="18px",
        padding_right="1.5rem",
    )


def _arc_inline_line(label: str, items: list[str]) -> rx.Component:
    return rx.cond(
        items,
        rx.hstack(
            rx.text(f"[{label}]", color=TEXT_TERTIARY, font_size="0.82rem", font_weight="600"),
            rx.text(_join_items(items), color=TEXT_SECONDARY, font_size="0.9rem", line_height="1.6", font_weight="400"),
            spacing="2",
            align="start",
            width="100%",
            wrap="wrap",
        ),
        rx.box(),
    )


def _arc_marker(is_active) -> rx.Component:
    return rx.box(
        rx.box(
            width="12px",
            height="12px",
            border_radius="999px",
            background=rx.cond(is_active, ACCENT, "#334155"),
            box_shadow=rx.cond(is_active, "0 0 12px rgba(56,189,248,0.75)", "none"),
            z_index="1",
            margin_top="0.1rem",
        ),
        rx.box(
            position="absolute",
            top="0",
            bottom="0",
            left="5px",
            width="2px",
            background=TRACK,
        ),
        width="40px",
        min_height="100%",
        position="relative",
        display="flex",
        justify_content="start",
        align_items="start",
    )


def _volume_arc_item(item: CharacterVolumeArc) -> rx.Component:
    is_highlighted = item.state_changes | item.relationship_changes
    return rx.hstack(
        _arc_marker(is_highlighted),
        rx.vstack(
            rx.hstack(
                rx.text(
                    rx.cond(item.volume_index > 0, "卷 " + item.volume_index.to_string(), "未分卷"),
                    color=ACCENT,
                    font_size="0.82rem",
                    font_weight="700",
                ),
                rx.spacer(),
                rx.text("Arc Node", color=TEXT_TERTIARY, font_size="0.74rem", text_transform="uppercase", font_weight="600"),
                width="100%",
            ),
            rx.text(
                rx.cond(item.volume_title, item.volume_title, "未命名卷"),
                color=TEXT_PRIMARY,
                font_size="1.08rem",
                font_weight="700",
            ),
            rx.text(item.summary, color=TEXT_SECONDARY, line_height="1.6", white_space="pre-wrap", font_weight="400"),
            _arc_inline_line("卷内定位", item.role_in_volume),
            _arc_inline_line("卷内目标", item.goals),
            _arc_inline_line("状态变化", item.state_changes),
            _arc_inline_line("关系变化", item.relationship_changes),
            width="100%",
            spacing="1",
            align="start",
            padding_bottom="1.75rem",
            border_bottom=DIVIDER,
        ),
        width="100%",
        align="start",
        spacing="0",
    )


def _arcs_tab_content(profile: CharacterProfileView) -> rx.Component:
    return rx.cond(
        profile.volume_arc,
        rx.box(
            rx.vstack(
                rx.foreach(profile.volume_arc, _volume_arc_item),
                spacing="0",
                width="100%",
                align="stretch",
            ),
            height=DETAIL_PANE_HEIGHT,
            overflow_y="auto",
            class_name=SCROLL_CLASS,
            padding=INNER_PANEL_PADDING,
            background=PANEL_BG,
            border_radius="18px",
            padding_right="1.5rem",
        ),
        _empty_state("No narrative arcs recorded."),
    )


def _relation_tab_value(item: CharacterRelationView):
    return "rel-" + item.target_character_name


def _emotion_relation_tab_value(item: CharacterEmotionalRelationView):
    return "emo-" + item.target_character_name


def _relation_dot_color(item: CharacterRelationView):
    polarity = rx.cond(item.history, item.history[0].polarity, "")
    return rx.cond(
        polarity == "negative",
        "#EF4444",
        rx.cond(polarity == "positive", ACCENT, TEXT_TERTIARY),
    )


def _emotional_timeline_row(item: CharacterEmotionalRelationTimeline) -> rx.Component:
    chapter_label = rx.cond(
        item.chapter_end > item.chapter_start,
        "Chapter " + item.chapter_start.to_string() + "-" + item.chapter_end.to_string(),
        "Chapter " + item.chapter_start.to_string(),
    )
    return rx.hstack(
        _arc_marker(True),
        rx.vstack(
            rx.text(chapter_label, color=ACCENT, font_size="0.8rem", font_weight="700"),
            rx.text(item.summary, color=TEXT_SECONDARY, line_height="1.68", white_space="pre-wrap", font_weight="400"),
            width="100%",
            spacing="1",
            align="start",
            padding_bottom="1.5rem",
            border_bottom=DIVIDER,
        ),
        width="100%",
        align="start",
        spacing="0",
    )


def _emotional_relation_detail_panel(item: CharacterEmotionalRelationView) -> rx.Component:
    return rx.vstack(
        rx.text(item.target_character_name, color=TEXT_PRIMARY, font_size="1.3rem", font_weight="800"),
        rx.cond(
            item.relation_summary,
            rx.text(item.relation_summary, color=TEXT_SECONDARY, line_height="1.72", white_space="pre-wrap", font_weight="400"),
            _empty_state("No emotional relation summary."),
        ),
        rx.cond(
            item.primary_relation_type,
            rx.hstack(
                rx.text("主类型", color=TEXT_TERTIARY, font_size="0.82rem", font_weight="600"),
                rx.text(item.primary_relation_type, color=TEXT_SECONDARY, font_size="0.9rem", font_weight="400"),
                spacing="2",
                wrap="wrap",
                width="100%",
            ),
            rx.box(),
        ),
        rx.cond(
            item.secondary_emotional_tendencies,
            rx.hstack(
                rx.text("次级倾向", color=TEXT_TERTIARY, font_size="0.82rem", font_weight="600"),
                rx.text(_join_items(item.secondary_emotional_tendencies), color=TEXT_SECONDARY, font_size="0.9rem", font_weight="400"),
                spacing="2",
                wrap="wrap",
                width="100%",
            ),
            rx.box(),
        ),
        rx.cond(item.intensity, rx.text("强度：" + item.intensity, color=TEXT_SECONDARY, font_size="0.82rem"), rx.box()),
        rx.cond(item.current_status, rx.text("当前状态：" + item.current_status, color=TEXT_SECONDARY, font_size="0.82rem"), rx.box()),
        rx.box(
            rx.cond(
                item.timeline,
                rx.vstack(
                    rx.foreach(item.timeline, _emotional_timeline_row),
                    spacing="0",
                    width="100%",
                    align="stretch",
                ),
                _empty_state("No emotional relation timeline recorded."),
            ),
            width="100%",
            max_height=RELATION_HISTORY_HEIGHT,
            overflow_y="auto",
            class_name=SCROLL_CLASS,
            padding_right="0.3rem",
        ),
        width="100%",
        spacing="3",
        align="stretch",
    )


def _emotional_relations_tab_content(profile: CharacterProfileView) -> rx.Component:
    return rx.cond(
        profile.emotional_relations,
        rx.tabs.root(
            rx.tabs.list(
                rx.foreach(
                    profile.emotional_relations,
                    lambda item: rx.tabs.trigger(
                        item.target_character_name,
                        value=_emotion_relation_tab_value(item),
                        color=TEXT_TERTIARY,
                        padding="0.45rem 0.15rem",
                        font_weight="600",
                        border_bottom="2px solid transparent",
                        _selected={"color": TEXT_PRIMARY, "border_bottom": f"2px solid {ACCENT}"},
                    ),
                ),
                width="100%",
                justify="start",
                gap="1.2rem",
                border_bottom=DIVIDER,
                padding_bottom="0.3rem",
                overflow_x="auto",
            ),
            rx.foreach(
                profile.emotional_relations,
                lambda item: rx.tabs.content(
                    _emotional_relation_detail_panel(item),
                    value=_emotion_relation_tab_value(item),
                    padding_top="1.2rem",
                    width="100%",
                ),
            ),
            default_value=_emotion_relation_tab_value(profile.emotional_relations[0]),
            width="100%",
        ),
        _empty_state("No emotional relation data recorded."),
    )


def _relation_meter(label: str, items: list[str], color: str = ACCENT) -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.text(label, color=TEXT_SECONDARY, font_size="0.88rem", font_weight="600"),
            rx.spacer(),
            rx.text(_normalized_score(items).to_string() + "%", color=TEXT_TERTIARY, font_size="0.8rem", font_weight="500"),
            width="100%",
            align="center",
        ),
        rx.progress(
            value=_normalized_score(items),
            max=100,
            width="100%",
            color_scheme="sky",
            radius="full",
            style={"--progress-indicator-bg": color},
        ),
        width="100%",
        spacing="1",
    )


def _relation_history_timeline_row(item: CharacterRelationHistory) -> rx.Component:
    chapter_label = rx.cond(
        item.chapter_end > item.chapter_start,
        "Chapter " + item.chapter_start.to_string() + "-" + item.chapter_end.to_string(),
        "Chapter " + item.chapter_start.to_string(),
    )
    return rx.hstack(
        _arc_marker(item.polarity == "positive"),
        rx.vstack(
            rx.hstack(
                rx.text(chapter_label, color=ACCENT, font_size="0.8rem", font_weight="700"),
                rx.spacer(),
                rx.text(item.relation_type, color=TEXT_TERTIARY, font_size="0.8rem", font_weight="600"),
                width="100%",
                align="center",
            ),
            rx.cond(
                item.summary,
                rx.text(item.summary, color=TEXT_SECONDARY, line_height="1.68", white_space="pre-wrap", font_weight="400"),
                _empty_state("No relation summary."),
            ),
            rx.hstack(
                rx.text("极性", color=TEXT_TERTIARY, font_size="0.8rem", font_weight="600"),
                rx.text(rx.cond(item.polarity, item.polarity, "neutral"), color=TEXT_SECONDARY, font_size="0.8rem", font_weight="400"),
                rx.text("强度", color=TEXT_TERTIARY, font_size="0.8rem", font_weight="600"),
                rx.text(rx.cond(item.strength, item.strength, "medium"), color=TEXT_SECONDARY, font_size="0.8rem", font_weight="400"),
                spacing="3",
                wrap="wrap",
                width="100%",
            ),
            rx.cond(item.directionality, rx.text("方向性：" + item.directionality, color=TEXT_SECONDARY, font_size="0.82rem", font_weight="400"), rx.box()),
            rx.cond(item.current_status, rx.text("阶段状态：" + item.current_status, color=TEXT_SECONDARY, font_size="0.82rem", font_weight="400"), rx.box()),
            rx.cond(item.stability, rx.text("稳定度：" + item.stability, color=TEXT_SECONDARY, font_size="0.82rem", font_weight="400"), rx.box()),
            rx.cond(
                item.drivers,
                rx.hstack(
                    rx.text("驱动因素", color=TEXT_TERTIARY, font_size="0.8rem", font_weight="600"),
                    rx.text(_join_items(item.drivers), color=TEXT_SECONDARY, font_size="0.82rem", font_weight="400"),
                    spacing="2",
                    wrap="wrap",
                    width="100%",
                ),
                rx.box(),
            ),
            width="100%",
            spacing="1",
            align="start",
            padding_bottom="1.5rem",
            border_bottom=DIVIDER,
        ),
        width="100%",
        align="start",
        spacing="0",
    )


def _relation_detail_panel(item: CharacterRelationView) -> rx.Component:
    return rx.vstack(
        rx.text(item.target_character_name, color=TEXT_PRIMARY, font_size="1.3rem", font_weight="800"),
        rx.cond(
            item.summary,
            rx.text(item.summary, color=TEXT_SECONDARY, line_height="1.72", white_space="pre-wrap", font_weight="400"),
            _empty_state("No overall relationship summary."),
        ),
        rx.vstack(
            _relation_meter("结构关系", item.structural_relation, "#38BDF8"),
            _relation_meter("行动关系", item.action_relation, "#0EA5E9"),
            _relation_meter("情感关系", item.emotional_relation, "#60A5FA"),
            spacing="3",
            width="100%",
        ),
        rx.box(
            rx.cond(
                item.history,
                rx.vstack(
                    rx.foreach(item.history, _relation_history_timeline_row),
                    spacing="0",
                    width="100%",
                    align="stretch",
                ),
                _empty_state("No relation timeline recorded."),
            ),
            width="100%",
            max_height=RELATION_HISTORY_HEIGHT,
            overflow_y="auto",
            class_name=SCROLL_CLASS,
            padding_right="0.4rem",
        ),
        width="100%",
        height="100%",
        min_height="0",
        spacing="3",
        align="start",
    )


def _relations_tab_content(relations: list[CharacterRelationView]) -> rx.Component:
    return rx.cond(
        relations,
        rx.tabs.root(
            rx.box(
                rx.grid(
                    rx.box(
                        rx.tabs.list(
                            rx.vstack(
                                rx.foreach(
                                    relations,
                                    lambda item: rx.tabs.trigger(
                                        rx.hstack(
                                            rx.box(
                                                width="8px",
                                                height="8px",
                                                border_radius="999px",
                                                background=_relation_dot_color(item),
                                                box_shadow="0 0 8px rgba(56,189,248,0.35)",
                                            ),
                                            rx.text(item.target_character_name, font_size="0.94rem", font_weight="500"),
                                            spacing="2",
                                            align="center",
                                            width="100%",
                                        ),
                                        value=_relation_tab_value(item),
                                        width="100%",
                                        justify_content="flex-start",
                                        color=TEXT_SECONDARY,
                                        background="transparent",
                                        border_radius="8px",
                                        padding="0.55rem 0.6rem",
                                        _hover={"background": "rgba(56,189,248,0.06)"},
                                        _selected={"color": TEXT_PRIMARY, "background": "rgba(56,189,248,0.12)"},
                                    ),
                                ),
                                width="100%",
                                spacing="1",
                                align="stretch",
                            ),
                            width="100%",
                        ),
                        width="100%",
                        height=DETAIL_PANE_HEIGHT,
                        overflow_y="auto",
                        class_name=SCROLL_CLASS,
                        border_right=DIVIDER,
                        padding_right="0.8rem",
                    ),
                    rx.box(
                        rx.tabs.content(
                            rx.flex(
                                _empty_state("请在左侧选择目标角色"),
                                width="100%",
                                height="100%",
                                align="center",
                                justify="center",
                            ),
                            value="__relation_empty__",
                            width="100%",
                            height="100%",
                        ),
                        rx.foreach(
                            relations,
                            lambda item: rx.tabs.content(
                                _relation_detail_panel(item),
                                value=_relation_tab_value(item),
                                width="100%",
                                height="100%",
                            ),
                        ),
                        width="100%",
                        height=DETAIL_PANE_HEIGHT,
                        padding_left="1.2rem",
                        overflow="hidden",
                    ),
                    width="100%",
                    grid_template_columns="3fr 7fr",
                    gap="0",
                ),
                width="100%",
                padding=INNER_PANEL_PADDING,
                background=PANEL_BG,
                border_radius="18px",
            ),
            default_value="__relation_empty__",
            width="100%",
        ),
        _empty_state("No relationship data recorded."),
    )


def _meta_pair(label: str, value) -> rx.Component:
    return rx.vstack(
        rx.text(label, color=TEXT_TERTIARY, font_size="0.74rem", text_transform="uppercase", font_weight="600", letter_spacing="0.08em"),
        rx.text(value, color=TEXT_PRIMARY, font_size="1.1rem", font_weight="700"),
        spacing="1",
        align="start",
    )


def _hero_header() -> rx.Component:
    return rx.vstack(
        rx.button(
            "← 返回角色档案",
            on_click=NovelState.open_character_archive,
            variant="ghost",
            color=TEXT_TERTIARY,
            border_radius="8px",
            padding="0",
            _hover={"background": "transparent", "color": TEXT_PRIMARY},
        ),
        rx.flex(
            rx.vstack(
                rx.text(
                    NovelState.current_character_card.name,
                    color=TEXT_PRIMARY,
                    font_size="3rem",
                    font_weight="800",
                    line_height="1.1",
                    letter_spacing="0.01em",
                ),
                rx.cond(
                    NovelState.current_character_card.alias_preview_text,
                    rx.text(
                        NovelState.current_character_card.alias_preview_text,
                        color=TEXT_TERTIARY,
                        font_size="1rem",
                        font_style="italic",
                        font_weight="400",
                    ),
                    _empty_state("No alias recorded."),
                ),
                width="100%",
                spacing="1",
                align="start",
            ),
            rx.hstack(
                _meta_pair("记录数", NovelState.current_character_card.record_count.to_string()),
                rx.box(width="1px", height="2.6rem", background="rgba(148,163,184,0.18)"),
                _meta_pair(
                    "登场区间",
                    "Ch." + NovelState.current_character_card.first_chapter_index.to_string() + " - " + NovelState.current_character_card.last_chapter_index.to_string(),
                ),
                rx.button(
                    NovelState.character_generate_button_text,
                    on_click=NovelState.generate_character_archive,
                    disabled=NovelState.character_generate_button_disabled,
                    background="linear-gradient(135deg, rgba(56,189,248,0.28) 0%, rgba(14,165,233,0.18) 100%)",
                    border=f"1px solid rgba(56,189,248,0.45)",
                    color=TEXT_PRIMARY,
                    border_radius="10px",
                    font_weight="700",
                    _hover={"background": "rgba(56,189,248,0.24)"},
                ),
                rx.button(
                    NovelState.character_roleplay_generate_button_text,
                    on_click=NovelState.generate_character_roleplay_enhancement,
                    disabled=NovelState.character_roleplay_generate_button_disabled,
                    background="linear-gradient(135deg, rgba(125,211,252,0.22) 0%, rgba(59,130,246,0.16) 100%)",
                    border="1px solid rgba(125,211,252,0.38)",
                    color=TEXT_PRIMARY,
                    border_radius="10px",
                    font_weight="700",
                    _hover={"background": "rgba(125,211,252,0.20)"},
                ),
                spacing="4",
                align="center",
                justify="end",
                wrap="wrap",
            ),
            width="100%",
            align="center",
            justify="between",
            gap="1.5rem",
            wrap="wrap",
            margin_top="1.5rem",
        ),
        width="100%",
        spacing="0",
        padding_bottom="1.4rem",
        border_bottom=DIVIDER,
    )


def character_detail_view() -> rx.Component:
    return rx.box(
        _scrollbar_style_tag(),
        rx.vstack(
            _hero_header(),
            rx.cond(
                NovelState.character_feedback_visible,
                rx.text(
                    NovelState.character_feedback,
                    color=rx.cond(NovelState.character_feedback_is_error, "#FB7185", "#67E8F9"),
                    font_size="0.9rem",
                    white_space="pre-wrap",
                    font_weight="400",
                ),
                rx.box(),
            ),
            rx.cond(
                NovelState.character_detail_loading,
                _empty_state("正在加载角色档案详情..."),
                rx.tabs.root(
                    rx.tabs.list(
                        rx.tabs.trigger(
                            "基础设定",
                            value="profile",
                            color=TEXT_TERTIARY,
                            padding="0.55rem 0.25rem",
                            border_radius="0",
                            border_bottom="2px solid transparent",
                            font_weight="600",
                            _selected={"color": TEXT_PRIMARY, "border_bottom": f"2px solid {ACCENT}", "box_shadow": "0 1px 10px rgba(56,189,248,0.22)"},
                        ),
                        rx.tabs.trigger(
                            "剧情轨迹",
                            value="arcs",
                            color=TEXT_TERTIARY,
                            padding="0.55rem 0.25rem",
                            border_radius="0",
                            border_bottom="2px solid transparent",
                            font_weight="600",
                            _selected={"color": TEXT_PRIMARY, "border_bottom": f"2px solid {ACCENT}", "box_shadow": "0 1px 10px rgba(56,189,248,0.22)"},
                        ),
                        rx.tabs.trigger(
                            "人物关系",
                            value="relations",
                            color=TEXT_TERTIARY,
                            padding="0.55rem 0.25rem",
                            border_radius="0",
                            border_bottom="2px solid transparent",
                            font_weight="600",
                            _selected={"color": TEXT_PRIMARY, "border_bottom": f"2px solid {ACCENT}", "box_shadow": "0 1px 10px rgba(56,189,248,0.22)"},
                        ),
                        rx.tabs.trigger(
                            "语言风格",
                            value="speech",
                            color=TEXT_TERTIARY,
                            padding="0.55rem 0.25rem",
                            border_radius="0",
                            border_bottom="2px solid transparent",
                            font_weight="600",
                            _selected={"color": TEXT_PRIMARY, "border_bottom": f"2px solid {ACCENT}", "box_shadow": "0 1px 10px rgba(56,189,248,0.22)"},
                        ),
                        rx.tabs.trigger(
                            "情感关系",
                            value="emotion",
                            color=TEXT_TERTIARY,
                            padding="0.55rem 0.25rem",
                            border_radius="0",
                            border_bottom="2px solid transparent",
                            font_weight="600",
                            _selected={"color": TEXT_PRIMARY, "border_bottom": f"2px solid {ACCENT}", "box_shadow": "0 1px 10px rgba(56,189,248,0.22)"},
                        ),
                        width="100%",
                        justify="start",
                        gap="1.5rem",
                        border_bottom=DIVIDER,
                        padding_bottom="0.3rem",
                    ),
                    rx.tabs.content(
                        _profile_tab_content(NovelState.current_character_profile),
                        value="profile",
                        padding_top="1.2rem",
                        width="100%",
                    ),
                    rx.tabs.content(
                        _arcs_tab_content(NovelState.current_character_profile),
                        value="arcs",
                        padding_top="1.2rem",
                        width="100%",
                    ),
                    rx.tabs.content(
                        _relations_tab_content(NovelState.current_character_relations),
                        value="relations",
                        padding_top="1.2rem",
                        width="100%",
                    ),
                    rx.tabs.content(
                        _speech_tab_content(NovelState.current_character_profile),
                        value="speech",
                        padding_top="1.2rem",
                        width="100%",
                    ),
                    rx.tabs.content(
                        _emotional_relations_tab_content(NovelState.current_character_profile),
                        value="emotion",
                        padding_top="1.2rem",
                        width="100%",
                    ),
                    default_value="profile",
                    width="100%",
                ),
            ),
            width="100%",
            spacing="4",
            align="stretch",
            padding_left=SAFE_AREA_X,
            padding_right=SAFE_AREA_X,
            padding_top="1.2rem",
            padding_bottom="1.1rem",
        ),
        width="100%",
        background=BG_APP,
    )
