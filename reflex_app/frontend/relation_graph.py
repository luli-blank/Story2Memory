from __future__ import annotations

import reflex as rx

from ..state import (
    NovelState,
    RelationGraphCanvasEdgeView,
    RelationGraphCanvasNodeView,
    RelationGraphEventDetailView,
)

GRAPH_VIEWBOX = "0 0 1000 640"
PAN_LAYER_DEFAULT_X = -280
PAN_LAYER_DEFAULT_Y = -220
PAN_WORLD_WIDTH = 1560
PAN_WORLD_HEIGHT = 1080
STAR_POINTS = (
    (72, 68, 1.2, 0.32),
    (144, 112, 1.4, 0.28),
    (238, 84, 1.1, 0.24),
    (312, 136, 1.6, 0.20),
    (418, 72, 1.2, 0.26),
    (534, 96, 1.4, 0.22),
    (648, 118, 1.0, 0.24),
    (774, 82, 1.6, 0.18),
    (862, 134, 1.2, 0.22),
    (930, 98, 1.0, 0.28),
    (124, 516, 1.2, 0.24),
    (212, 566, 1.5, 0.18),
    (326, 544, 1.1, 0.28),
    (434, 592, 1.6, 0.18),
    (544, 556, 1.1, 0.22),
    (632, 604, 1.3, 0.20),
    (748, 562, 1.5, 0.18),
    (842, 596, 1.2, 0.22),
    (926, 548, 1.1, 0.24),
)


def _graph_shell(*children) -> rx.Component:
    return rx.box(
        *children,
        width="100%",
        min_height="calc(100vh - 4rem)",
        padding="1.2rem 1.4rem",
        border_radius="24px",
        background="radial-gradient(circle at 20% 20%, rgba(56,189,248,0.14), transparent 28%), radial-gradient(circle at 85% 15%, rgba(59,130,246,0.12), transparent 26%), linear-gradient(180deg, rgba(8,15,30,0.92) 0%, rgba(2,6,23,0.98) 100%)",
        border="1px solid rgba(148,163,184,0.10)",
        box_shadow="0 24px 80px rgba(2,6,23,0.52)",
    )


def _detail_label(text: str) -> rx.Component:
    return rx.text(text, color="#64748B", font_size="0.76rem", text_transform="uppercase", letter_spacing="0.08em")


def _detail_value(text) -> rx.Component:
    return rx.text(text, color="#0F172A", font_size="0.94rem", line_height="1.65")


def _node_sidebar() -> rx.Component:
    return rx.vstack(
        rx.text("人物信息", color="#0F172A", font_size="1.18rem", font_weight="800"),
        _detail_label("角色名"),
        _detail_value(NovelState.relation_graph_node_detail.name),
        _detail_label("状态"),
        _detail_value(rx.cond(NovelState.relation_graph_node_detail.profile_status == "ready", "已生成画像", "占位节点")),
        rx.cond(
            NovelState.relation_graph_node_alias_text,
            rx.vstack(_detail_label("别名"), _detail_value(NovelState.relation_graph_node_alias_text), width="100%", spacing="1"),
            rx.box(),
        ),
        rx.vstack(_detail_label("章节范围"), _detail_value(NovelState.relation_graph_node_chapter_range), width="100%", spacing="1"),
        rx.cond(
            NovelState.relation_graph_node_detail.current_state_summary,
            rx.vstack(_detail_label("画像摘要"), _detail_value(NovelState.relation_graph_node_detail.current_state_summary), width="100%", spacing="1"),
            rx.box(),
        ),
        width="100%",
        spacing="3",
        align="start",
    )


def _history_item(item: RelationGraphEventDetailView) -> rx.Component:
    return rx.box(
        rx.vstack(
            rx.hstack(
                rx.text(
                    rx.cond(
                        item.chapter_end > item.chapter_start,
                        "Ch." + item.chapter_start.to_string() + "-" + item.chapter_end.to_string(),
                        "Ch." + item.chapter_start.to_string(),
                    ),
                    color="#0284C7",
                    font_size="0.8rem",
                    font_weight="700",
                ),
                rx.spacer(),
                rx.text(item.relation_type, color="#64748B", font_size="0.78rem"),
                width="100%",
            ),
            rx.text(item.summary, color="#0F172A", font_size="0.88rem", line_height="1.6", white_space="pre-wrap"),
            rx.hstack(
                rx.text("极性", color="#64748B", font_size="0.76rem"),
                rx.text(item.polarity, color="#334155", font_size="0.78rem"),
                rx.text("强度", color="#64748B", font_size="0.76rem"),
                rx.text(item.strength, color="#334155", font_size="0.78rem"),
                spacing="3",
                wrap="wrap",
                width="100%",
            ),
            rx.cond(item.directionality, rx.text("方向性：" + item.directionality, color="#334155", font_size="0.8rem"), rx.box()),
            rx.cond(item.current_status, rx.text("状态：" + item.current_status, color="#334155", font_size="0.8rem"), rx.box()),
            width="100%",
            spacing="1",
            align="start",
        ),
        width="100%",
        padding="0.85rem 0 1rem 0",
        border_bottom="1px solid rgba(15,23,42,0.08)",
    )


def _edge_sidebar() -> rx.Component:
    return rx.vstack(
        rx.text("人物关系", color="#0F172A", font_size="1.18rem", font_weight="800"),
        _detail_label("主客体"),
        _detail_value(NovelState.relation_graph_edge_detail.source_name + " → " + NovelState.relation_graph_edge_detail.target_name),
        rx.vstack(_detail_label("关系跨度"), _detail_value(NovelState.relation_graph_edge_chapter_range), width="100%", spacing="1"),
        rx.cond(
            NovelState.relation_graph_edge_detail.summary,
            rx.vstack(_detail_label("总览"), _detail_value(NovelState.relation_graph_edge_detail.summary), width="100%", spacing="1"),
            rx.box(),
        ),
        rx.cond(
            NovelState.relation_graph_edge_detail.structural_relation,
            rx.vstack(_detail_label("结构关系"), _detail_value(NovelState.relation_graph_edge_detail.structural_relation.join(" / ")), width="100%", spacing="1"),
            rx.box(),
        ),
        rx.cond(
            NovelState.relation_graph_edge_detail.action_relation,
            rx.vstack(_detail_label("行动关系"), _detail_value(NovelState.relation_graph_edge_detail.action_relation.join(" / ")), width="100%", spacing="1"),
            rx.box(),
        ),
        rx.cond(
            NovelState.relation_graph_edge_detail.emotional_relation,
            rx.vstack(_detail_label("情感关系"), _detail_value(NovelState.relation_graph_edge_detail.emotional_relation.join(" / ")), width="100%", spacing="1"),
            rx.box(),
        ),
        rx.cond(
            NovelState.relation_graph_edge_detail.drivers,
            rx.vstack(_detail_label("驱动因素"), _detail_value(NovelState.relation_graph_edge_detail.drivers.join(" / ")), width="100%", spacing="1"),
            rx.box(),
        ),
        rx.cond(
            NovelState.relation_graph_edge_detail.history,
            rx.vstack(
                _detail_label("动态历史"),
                rx.box(
                    rx.vstack(
                        rx.foreach(NovelState.relation_graph_edge_detail.history, _history_item),
                        width="100%",
                        spacing="0",
                        align="stretch",
                    ),
                    width="100%",
                    max_height="42vh",
                    overflow_y="auto",
                    padding_right="0.25rem",
                ),
                width="100%",
                spacing="1",
            ),
            rx.box(),
        ),
        width="100%",
        spacing="3",
        align="start",
    )


def _empty_sidebar() -> rx.Component:
    return rx.vstack(
        rx.text("Graph Inspector", color="#0F172A", font_size="1.1rem", font_weight="800"),
        rx.text("单击人物节点查看人物信息，单击连线查看两名角色之间的关系与动态历史。", color="#475569", line_height="1.7"),
        width="100%",
        spacing="3",
        align="start",
    )


def _graph_pan_script() -> str:
    return f"""
(() => {{
  const selector = '[data-relation-viewport="true"]';
  const initViewport = (viewport) => {{
    if (!viewport || viewport.dataset.relationGraphPanReady === 'true') return;
    const layer = viewport.querySelector('[data-pan-layer="true"]');
    if (!layer) return;
    viewport.dataset.relationGraphPanReady = 'true';
    const viewWidth = 1000;
    const viewHeight = 640;
    const worldWidth = {PAN_WORLD_WIDTH};
    const worldHeight = {PAN_WORLD_HEIGHT};
    const state = {{
      x: {PAN_LAYER_DEFAULT_X},
      y: {PAN_LAYER_DEFAULT_Y},
      scale: 1,
      dragging: false,
      startX: 0,
      startY: 0,
      originX: {PAN_LAYER_DEFAULT_X},
      originY: {PAN_LAYER_DEFAULT_Y},
    }};

    const clamp = () => {{
      const scaledWidth = worldWidth * state.scale;
      const scaledHeight = worldHeight * state.scale;
      const minX = scaledWidth <= viewWidth ? (viewWidth - scaledWidth) / 2 : viewWidth - scaledWidth;
      const maxX = scaledWidth <= viewWidth ? (viewWidth - scaledWidth) / 2 : 0;
      const minY = scaledHeight <= viewHeight ? (viewHeight - scaledHeight) / 2 : viewHeight - scaledHeight;
      const maxY = scaledHeight <= viewHeight ? (viewHeight - scaledHeight) / 2 : 0;
      state.x = Math.min(maxX, Math.max(minX, state.x));
      state.y = Math.min(maxY, Math.max(minY, state.y));
    }};

    const apply = () => {{
      clamp();
      layer.setAttribute('transform', `translate(${{state.x.toFixed(2)}} ${{state.y.toFixed(2)}}) scale(${{state.scale.toFixed(3)}})`);
      viewport.style.cursor = state.dragging ? 'grabbing' : 'grab';
    }};

    viewport.addEventListener('wheel', (event) => {{
      event.preventDefault();
      const rect = viewport.getBoundingClientRect();
      const localX = event.clientX - rect.left;
      const localY = event.clientY - rect.top;
      const previousScale = state.scale;
      const nextScale = Math.min(1.85, Math.max(0.72, previousScale * (event.deltaY < 0 ? 1.08 : 0.92)));
      if (nextScale === previousScale) return;
      const worldX = (localX - state.x) / previousScale;
      const worldY = (localY - state.y) / previousScale;
      state.scale = nextScale;
      state.x = localX - (worldX * nextScale);
      state.y = localY - (worldY * nextScale);
      apply();
    }}, {{ passive: false }});

    viewport.addEventListener('pointerdown', (event) => {{
      const interactiveTarget = event.target?.closest('[data-node-clickable="true"], [data-edge-clickable="true"]');
      if (interactiveTarget) return;
      state.dragging = true;
      state.startX = event.clientX;
      state.startY = event.clientY;
      state.originX = state.x;
      state.originY = state.y;
      try {{ viewport.setPointerCapture(event.pointerId); }} catch (_error) {{}}
      apply();
    }});

    viewport.addEventListener('pointermove', (event) => {{
      if (!state.dragging) return;
      state.x = state.originX + (event.clientX - state.startX);
      state.y = state.originY + (event.clientY - state.startY);
      apply();
    }});

    const stopDragging = (event) => {{
      if (!state.dragging) return;
      state.dragging = false;
      try {{ viewport.releasePointerCapture(event.pointerId); }} catch (_error) {{}}
      apply();
    }};

    viewport.addEventListener('pointerup', stopDragging);
    viewport.addEventListener('pointercancel', stopDragging);
    viewport.addEventListener('mouseleave', (event) => {{
      if (event.buttons === 0) stopDragging(event);
    }});

    apply();
  }};

  const boot = () => document.querySelectorAll(selector).forEach(initViewport);
  boot();
  if (!window.__relationGraphPanBooted) {{
    const observer = new MutationObserver(() => boot());
    observer.observe(document.body, {{ childList: true, subtree: true }});
    window.__relationGraphPanBooted = true;
  }}
}})();
"""


def _graph_defs() -> rx.Component:
    return rx.el.svg.defs(
        rx.el.svg.radial_gradient(
            rx.el.svg.stop(offset="0%", stop_color="#E0FBFF", stop_opacity="0.98"),
            rx.el.svg.stop(offset="58%", stop_color="#64D8FF", stop_opacity="0.96"),
            rx.el.svg.stop(offset="100%", stop_color="#1D4ED8", stop_opacity="0.92"),
            id="graph-node-core",
        ),
        rx.el.svg.radial_gradient(
            rx.el.svg.stop(offset="0%", stop_color="#F8FDFF", stop_opacity="1"),
            rx.el.svg.stop(offset="54%", stop_color="#7DEBFF", stop_opacity="0.98"),
            rx.el.svg.stop(offset="100%", stop_color="#0EA5E9", stop_opacity="0.94"),
            id="graph-node-selected",
        ),
        rx.el.svg.radial_gradient(
            rx.el.svg.stop(offset="0%", stop_color="#E8FCFF", stop_opacity="0.96"),
            rx.el.svg.stop(offset="60%", stop_color="#59C8FF", stop_opacity="0.92"),
            rx.el.svg.stop(offset="100%", stop_color="#2563EB", stop_opacity="0.88"),
            id="graph-node-linked",
        ),
        rx.el.svg.radial_gradient(
            rx.el.svg.stop(offset="0%", stop_color="#E5EDF8", stop_opacity="0.96"),
            rx.el.svg.stop(offset="62%", stop_color="#94A3B8", stop_opacity="0.92"),
            rx.el.svg.stop(offset="100%", stop_color="#475569", stop_opacity="0.86"),
            id="graph-node-stub",
        ),
    )


def _graph_stars() -> list[rx.Component]:
    return [
        rx.el.svg.circle(
            cx=x,
            cy=y,
            r=str(r),
            fill="#D8F3FF",
            opacity=str(opacity),
        )
        for x, y, r, opacity in STAR_POINTS
    ]


def _graph_edge(item: RelationGraphCanvasEdgeView) -> rx.Component:
    is_selected = NovelState.relation_graph_selected_edge_key == item.edge_key
    is_emphasized = NovelState.relation_graph_highlighted_edge_keys.contains(item.edge_key)
    is_clickable = NovelState.relation_graph_clickable_edge_keys.contains(item.edge_key)
    return rx.el.svg.g(
        rx.el.svg.path(
            d=item.path_d,
            fill="none",
            stroke=rx.cond(is_selected, "rgba(103,232,249,0.32)", rx.cond(is_emphasized, "rgba(56,189,248,0.20)", "rgba(15,23,42,0.0)")),
            stroke_width=rx.cond(is_selected, "12", rx.cond(is_emphasized, "8", "0")),
            stroke_linecap="round",
        ),
        rx.el.svg.path(
            d=item.path_d,
            fill="none",
            stroke=rx.cond(is_selected, "#67E8F9", rx.cond(is_emphasized, item.color, "rgba(71,85,105,0.16)")),
            stroke_width=rx.cond(is_selected, "6.5", rx.cond(is_emphasized, "4.2", "1.7")),
            stroke_linecap="round",
            opacity=rx.cond(is_selected, "1", rx.cond(is_emphasized, "0.96", "0.18")),
        ),
        rx.cond(
            is_clickable,
            rx.el.svg.path(
                d=item.path_d,
                fill="none",
                stroke="rgba(255,255,255,0.02)",
                stroke_width=rx.cond(is_selected, "24", rx.cond(is_emphasized, "22", "18")),
                stroke_linecap="round",
                cursor="pointer",
                data_edge_clickable="true",
                on_click=NovelState.select_relation_graph_edge(item.edge_key),
            ),
            rx.el.svg.path(
                d=item.path_d,
                fill="none",
                stroke="rgba(255,255,255,0)",
                stroke_width="0",
                pointer_events="none",
            ),
        ),
        data_edge_clickable="true",
    )


def _graph_node(item: RelationGraphCanvasNodeView) -> rx.Component:
    is_selected = NovelState.relation_graph_selected_node_key == item.graph_key
    is_linked = NovelState.relation_graph_highlighted_node_keys.contains(item.graph_key)
    return rx.el.svg.g(
        rx.el.svg.circle(
            cx=item.x,
            cy=item.y,
            r=rx.cond(is_selected, item.radius + 18, rx.cond(is_linked, item.radius + 14, item.radius + 8)),
            fill=rx.cond(
                is_selected,
                "rgba(125,242,255,0.26)",
                rx.cond(is_linked, "rgba(89,200,255,0.16)", "rgba(56,189,248,0.05)"),
            ),
        ),
        rx.el.svg.circle(
            cx=item.x,
            cy=item.y,
            r=rx.cond(is_selected, item.radius + 13, rx.cond(is_linked, item.radius + 10, item.radius + 5)),
            fill=rx.cond(
                is_selected,
                "rgba(103,232,249,0.28)",
                rx.cond(is_linked, "rgba(56,189,248,0.16)", "rgba(56,189,248,0.06)"),
            ),
            cursor="pointer",
            on_click=NovelState.select_relation_graph_node(item.graph_key),
        ),
        rx.el.svg.circle(
            cx=item.x,
            cy=item.y,
            r=rx.cond(is_selected, item.radius + 6, rx.cond(is_linked, item.radius + 4, item.radius + 1)),
            fill="none",
            stroke=rx.cond(is_selected, "rgba(224,242,254,0.82)", rx.cond(is_linked, "rgba(125,211,252,0.65)", "rgba(255,255,255,0.08)")),
            stroke_width=rx.cond(is_selected, "2.8", rx.cond(is_linked, "2.2", "1")),
        ),
        rx.el.svg.circle(
            cx=item.x,
            cy=item.y,
            r=item.radius,
            fill=rx.cond(
                is_selected,
                "url(#graph-node-selected)",
                rx.cond(
                    is_linked,
                    "url(#graph-node-linked)",
                    rx.cond(item.color == "#94A3B8", "url(#graph-node-stub)", "url(#graph-node-core)"),
                ),
            ),
            stroke=rx.cond(is_selected, "#E0F2FE", rx.cond(is_linked, "#BAE6FD", "rgba(255,255,255,0.16)")),
            stroke_width=rx.cond(is_selected, "4.5", rx.cond(is_linked, "3.25", "2")),
            cursor="pointer",
            data_node_clickable="true",
            on_click=NovelState.select_relation_graph_node(item.graph_key),
        ),
        rx.el.svg.circle(
            cx=item.x,
            cy=item.y,
            r=rx.cond(is_selected, "10", rx.cond(is_linked, "8", "6")),
            fill="rgba(255,255,255,0.18)",
            opacity=rx.cond(is_selected, "0.48", rx.cond(is_linked, "0.26", "0.16")),
            pointer_events="none",
        ),
        rx.el.svg.text(
            item.name,
            x=item.x,
            y=item.label_y,
            text_anchor="middle",
            fill=rx.cond(is_selected, "#F8FAFC", rx.cond(is_linked, "#CFFAFE", "#8AD9F5")),
            font_size=rx.cond(is_selected, "13", "12"),
            font_weight=rx.cond(is_selected, "700", rx.cond(is_linked, "650", "600")),
            stroke="rgba(2,6,23,0.72)",
            stroke_width="0.65",
            paint_order="stroke",
            cursor="pointer",
            data_node_clickable="true",
            on_click=NovelState.select_relation_graph_node(item.graph_key),
        ),
        data_node_clickable="true",
    )


def _graph_canvas() -> rx.Component:
    return rx.box(
        rx.box(
            rx.el.svg(
                _graph_defs(),
                *_graph_stars(),
                rx.el.svg.g(
                    rx.foreach(NovelState.relation_graph_canvas_edges, _graph_edge),
                    rx.foreach(NovelState.relation_graph_canvas_nodes, _graph_node),
                    data_pan_layer="true",
                    transform=f"translate({PAN_LAYER_DEFAULT_X} {PAN_LAYER_DEFAULT_Y}) scale(1)",
                ),
                view_box=GRAPH_VIEWBOX,
                width="100%",
                height="100%",
            ),
            width="100%",
            height="100%",
            position="absolute",
            inset="0",
        ),
        rx.script(_graph_pan_script()),
        rx.vstack(
            rx.text("Star Relational Field", color="#D8F3FF", font_size="0.72rem", font_weight="700", letter_spacing="0.18em", text_transform="uppercase"),
            rx.text(
                rx.cond(
                    NovelState.relation_graph_selected_kind == "node",
                    "已高亮当前角色的相连关系",
                    rx.cond(
                        NovelState.relation_graph_selected_kind == "edge",
                        "已高亮当前关系的双端角色",
                        "单击角色或关系线查看细节",
                    ),
                ),
                color="#A5B4C8",
                font_size="0.82rem",
                line_height="1.5",
            ),
            position="absolute",
            top="1rem",
            left="1rem",
            z_index="2",
            spacing="1",
            align="start",
            padding="0.7rem 0.85rem",
            border_radius="14px",
            background="rgba(2,6,23,0.46)",
            border="1px solid rgba(125,211,252,0.10)",
            backdrop_filter="blur(10px)",
        ),
        rx.hstack(
            rx.box(width="0.55rem", height="0.55rem", border_radius="999px", background="#67E8F9"),
            rx.text("节点选中后，相关边会自动增强；边选中后，两端节点会同步增强。", color="#CBD5E1", font_size="0.76rem"),
            position="absolute",
            left="1rem",
            bottom="1rem",
            z_index="2",
            spacing="2",
            padding="0.65rem 0.8rem",
            border_radius="999px",
            background="rgba(15,23,42,0.62)",
            border="1px solid rgba(148,163,184,0.12)",
            backdrop_filter="blur(8px)",
            align="center",
        ),
        width="100%",
        height="100%",
        position="relative",
        border_radius="24px",
        background="radial-gradient(circle at 18% 22%, rgba(56,189,248,0.18), transparent 24%), radial-gradient(circle at 82% 18%, rgba(14,165,233,0.22), transparent 28%), radial-gradient(circle at 50% 50%, rgba(56,189,248,0.10), transparent 18%), linear-gradient(180deg, rgba(6,11,24,0.70) 0%, rgba(2,6,23,0.96) 100%)",
        border="1px solid rgba(148,163,184,0.14)",
        box_shadow="inset 0 1px 0 rgba(255,255,255,0.04), 0 30px 80px rgba(2,6,23,0.42)",
        overflow="hidden",
        data_relation_viewport="true",
        touch_action="none",
    )


def relation_graph_view() -> rx.Component:
    return rx.vstack(
        rx.hstack(
            rx.button(
                "← 返回书籍详情",
                on_click=NovelState.back_to_book_detail,
                variant="ghost",
                color="#94A3B8",
                _hover={"background": "rgba(56,189,248,0.12)", "color": "white"},
            ),
            rx.spacer(),
            rx.text("关系图", color="#E0F2FE", font_size="1.35rem", font_weight="800", letter_spacing="0.02em"),
            width="100%",
            align="center",
        ),
        _graph_shell(
            rx.grid(
                rx.box(
                    rx.cond(
                        NovelState.relation_graph_loading,
                        rx.flex(rx.text("正在加载关系图谱...", color="#BFDBFE"), align="center", justify="center", width="100%", height="100%"),
                        rx.cond(
                            NovelState.relation_graph_error,
                            rx.flex(rx.text(NovelState.relation_graph_error, color="#FECACA", white_space="pre-wrap"), align="center", justify="center", width="100%", height="100%"),
                            rx.cond(
                                NovelState.relation_graph_empty,
                                rx.flex(rx.text("当前书籍暂无可展示的关系图谱。请先生成部分角色画像。", color="#94A3B8"), align="center", justify="center", width="100%", height="100%"),
                                _graph_canvas(),
                            ),
                        ),
                    ),
                    width="100%",
                    height="calc(100vh - 11rem)",
                ),
                rx.box(
                    rx.cond(
                        NovelState.relation_graph_selected_kind == "node",
                        _node_sidebar(),
                        rx.cond(
                            NovelState.relation_graph_selected_kind == "edge",
                            _edge_sidebar(),
                            _empty_sidebar(),
                        ),
                    ),
                    width="100%",
                    height="calc(100vh - 11rem)",
                    overflow_y="auto",
                    padding="1.25rem 1.35rem",
                    border_radius="18px",
                    background="rgba(248,250,252,0.92)",
                    box_shadow="0 18px 42px rgba(15,23,42,0.28)",
                    border="1px solid rgba(255,255,255,0.42)",
                ),
                width="100%",
                grid_template_columns="minmax(0, 1fr) 340px",
                gap="1.25rem",
            )
        ),
        width="100%",
        spacing="4",
        align="stretch",
    )
