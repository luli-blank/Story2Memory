from __future__ import annotations

from collections import defaultdict
from typing import Any

import networkx as nx

from .client import run_read
from .models import EdgeDetail, GraphEdge, GraphNode, NodeDetail, RelationEvent


def _node_color(profile_status: str) -> str:
    return "#38BDF8" if str(profile_status or "") == "ready" else "#94A3B8"


def _edge_color(current_status: str) -> str:
    status = str(current_status or "").strip()
    if "敌" in status or "冲突" in status:
        return "rgba(248,113,113,0.72)"
    if "盟" in status or "信任" in status or "合作" in status:
        return "rgba(56,189,248,0.72)"
    return "rgba(148,163,184,0.5)"


def _read_book_relation_graph(tx, book_id: int) -> dict[str, Any]:
    nodes_result = tx.run(
        """
        MATCH (c:Character {book_id: $book_id})
        RETURN c.graph_key AS graph_key,
               c.book_id AS book_id,
               c.character_id AS character_id,
               c.name AS name,
               coalesce(c.aliases, []) AS aliases,
               coalesce(c.profile_status, 'stub') AS profile_status,
               coalesce(c.current_state_summary, '') AS current_state_summary,
               coalesce(c.first_chapter_index, 0) AS first_chapter_index,
               coalesce(c.last_chapter_index, 0) AS last_chapter_index,
               coalesce(c.version_hash, '') AS version_hash
        ORDER BY c.name ASC
        """,
        book_id=int(book_id),
    )
    edges_result = tx.run(
        """
        MATCH (s:Character {book_id: $book_id})-[r:RELATES_TO]->(t:Character {book_id: $book_id})
        RETURN r.edge_key AS edge_key,
               r.book_id AS book_id,
               s.graph_key AS source_graph_key,
               t.graph_key AS target_graph_key,
               s.name AS source_name,
               t.name AS target_name,
               coalesce(r.summary, '') AS summary,
               coalesce(r.structural_relation, []) AS structural_relation,
               coalesce(r.action_relation, []) AS action_relation,
               coalesce(r.emotional_relation, []) AS emotional_relation,
               coalesce(r.directionality, '') AS directionality,
               coalesce(r.stability, '') AS stability,
               coalesce(r.current_status, '') AS current_status,
               coalesce(r.drivers, []) AS drivers,
               coalesce(r.first_chapter_index, 0) AS first_chapter_index,
               coalesce(r.last_chapter_index, 0) AS last_chapter_index,
               coalesce(r.version_hash, '') AS version_hash
        ORDER BY s.name ASC, t.name ASC
        """,
        book_id=int(book_id),
    )
    return {
        "nodes": [record.data() for record in nodes_result],
        "edges": [record.data() for record in edges_result],
    }


def _read_node_detail(tx, book_id: int, graph_key: str) -> dict[str, Any] | None:
    result = tx.run(
        """
        MATCH (c:Character {book_id: $book_id, graph_key: $graph_key})
        RETURN c.graph_key AS graph_key,
               c.book_id AS book_id,
               c.character_id AS character_id,
               c.name AS name,
               coalesce(c.aliases, []) AS aliases,
               coalesce(c.profile_status, 'stub') AS profile_status,
               coalesce(c.current_state_summary, '') AS current_state_summary,
               coalesce(c.first_chapter_index, 0) AS first_chapter_index,
               coalesce(c.last_chapter_index, 0) AS last_chapter_index,
               coalesce(c.version_hash, '') AS version_hash
        LIMIT 1
        """,
        book_id=int(book_id),
        graph_key=str(graph_key),
    ).single()
    return result.data() if result else None


def _read_edge_detail(tx, book_id: int, edge_key: str) -> dict[str, Any] | None:
    edge_record = tx.run(
        """
        MATCH (s:Character {book_id: $book_id})-[r:RELATES_TO {edge_key: $edge_key}]->(t:Character {book_id: $book_id})
        RETURN r.edge_key AS edge_key,
               r.book_id AS book_id,
               s.graph_key AS source_graph_key,
               t.graph_key AS target_graph_key,
               s.name AS source_name,
               t.name AS target_name,
               coalesce(r.summary, '') AS summary,
               coalesce(r.structural_relation, []) AS structural_relation,
               coalesce(r.action_relation, []) AS action_relation,
               coalesce(r.emotional_relation, []) AS emotional_relation,
               coalesce(r.directionality, '') AS directionality,
               coalesce(r.stability, '') AS stability,
               coalesce(r.current_status, '') AS current_status,
               coalesce(r.drivers, []) AS drivers
        LIMIT 1
        """,
        book_id=int(book_id),
        edge_key=str(edge_key),
    ).single()
    if not edge_record:
        return None

    history_result = tx.run(
        """
        MATCH (e:RelationEvent {book_id: $book_id, edge_key: $edge_key})
        RETURN e.event_key AS event_key,
               e.book_id AS book_id,
               e.edge_key AS edge_key,
               e.source_graph_key AS source_graph_key,
               e.target_graph_key AS target_graph_key,
               coalesce(e.chapter_start, 0) AS chapter_start,
               coalesce(e.chapter_end, 0) AS chapter_end,
               coalesce(e.relation_type, '') AS relation_type,
               coalesce(e.polarity, 'neutral') AS polarity,
               coalesce(e.strength, 'medium') AS strength,
               coalesce(e.directionality, '') AS directionality,
               coalesce(e.stability, '') AS stability,
               coalesce(e.current_status, '') AS current_status,
               coalesce(e.summary, '') AS summary,
               coalesce(e.evidence_chapters, []) AS evidence_chapters
        ORDER BY e.chapter_start ASC, e.chapter_end ASC
        """,
        book_id=int(book_id),
        edge_key=str(edge_key),
    )
    payload = edge_record.data()
    payload["history"] = [record.data() for record in history_result]
    return payload


def _build_layout(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> tuple[list[GraphNode], list[GraphEdge]]:
    graph = nx.MultiDiGraph()
    for node in nodes:
        graph.add_node(str(node["graph_key"]))
    for edge in edges:
        graph.add_edge(str(edge["source_graph_key"]), str(edge["target_graph_key"]), key=str(edge["edge_key"]))

    if graph.number_of_nodes() > 0:
        positions = nx.spring_layout(graph, seed=11, k=1.4 / max(1, graph.number_of_nodes() ** 0.5))
    else:
        positions = {}

    degree_by_key = defaultdict(int)
    for edge in edges:
        degree_by_key[str(edge["source_graph_key"])] += 1
        degree_by_key[str(edge["target_graph_key"])] += 1

    graph_nodes = [
        GraphNode(
            graph_key=str(node["graph_key"]),
            book_id=int(node["book_id"] or 0),
            character_id=int(node["character_id"]) if node.get("character_id") is not None else None,
            name=str(node["name"] or ""),
            aliases=[str(item or "").strip() for item in node.get("aliases") or [] if str(item or "").strip()],
            profile_status=str(node.get("profile_status") or "stub"),
            current_state_summary=str(node.get("current_state_summary") or ""),
            first_chapter_index=int(node.get("first_chapter_index") or 0),
            last_chapter_index=int(node.get("last_chapter_index") or 0),
            version_hash=str(node.get("version_hash") or ""),
            degree=int(degree_by_key[str(node["graph_key"])]),
            x=float(positions.get(str(node["graph_key"]), (0.0, 0.0))[0]),
            y=float(positions.get(str(node["graph_key"]), (0.0, 0.0))[1]),
            size=18.0 + min(20.0, degree_by_key[str(node["graph_key"])] * 3.0),
            color=_node_color(str(node.get("profile_status") or "stub")),
        )
        for node in nodes
    ]
    node_lookup = {item.graph_key: item for item in graph_nodes}

    graph_edges = [
        GraphEdge(
            edge_key=str(edge["edge_key"]),
            book_id=int(edge["book_id"] or 0),
            source_graph_key=str(edge["source_graph_key"]),
            target_graph_key=str(edge["target_graph_key"]),
            source_name=str(edge["source_name"] or node_lookup[str(edge["source_graph_key"])].name),
            target_name=str(edge["target_name"] or node_lookup[str(edge["target_graph_key"])].name),
            summary=str(edge.get("summary") or ""),
            structural_relation=[str(item or "").strip() for item in edge.get("structural_relation") or [] if str(item or "").strip()],
            action_relation=[str(item or "").strip() for item in edge.get("action_relation") or [] if str(item or "").strip()],
            emotional_relation=[str(item or "").strip() for item in edge.get("emotional_relation") or [] if str(item or "").strip()],
            directionality=str(edge.get("directionality") or ""),
            stability=str(edge.get("stability") or ""),
            current_status=str(edge.get("current_status") or ""),
            drivers=[str(item or "").strip() for item in edge.get("drivers") or [] if str(item or "").strip()],
            first_chapter_index=int(edge.get("first_chapter_index") or 0),
            last_chapter_index=int(edge.get("last_chapter_index") or 0),
            version_hash=str(edge.get("version_hash") or ""),
            color=_edge_color(str(edge.get("current_status") or "")),
        )
        for edge in edges
    ]
    return graph_nodes, graph_edges


def load_book_relation_graph(book_id: int) -> dict[str, Any]:
    raw = run_read(_read_book_relation_graph, int(book_id))
    graph_nodes, graph_edges = _build_layout(raw.get("nodes", []), raw.get("edges", []))
    return {
        "nodes": [node.to_dict() for node in graph_nodes],
        "edges": [edge.to_dict() for edge in graph_edges],
    }


def load_character_node_detail(book_id: int, graph_key: str) -> dict[str, Any]:
    raw = run_read(_read_node_detail, int(book_id), str(graph_key))
    if raw is None:
        return {}
    return NodeDetail(
        graph_key=str(raw["graph_key"]),
        book_id=int(raw["book_id"] or 0),
        character_id=int(raw["character_id"]) if raw.get("character_id") is not None else None,
        name=str(raw["name"] or ""),
        aliases=[str(item or "").strip() for item in raw.get("aliases") or [] if str(item or "").strip()],
        profile_status=str(raw.get("profile_status") or "stub"),
        current_state_summary=str(raw.get("current_state_summary") or ""),
        first_chapter_index=int(raw.get("first_chapter_index") or 0),
        last_chapter_index=int(raw.get("last_chapter_index") or 0),
        version_hash=str(raw.get("version_hash") or ""),
    ).to_dict()


def load_relation_edge_detail(book_id: int, edge_key: str) -> dict[str, Any]:
    raw = run_read(_read_edge_detail, int(book_id), str(edge_key))
    if raw is None:
        return {}
    history = [
        RelationEvent(
            event_key=str(item["event_key"]),
            book_id=int(item["book_id"] or 0),
            edge_key=str(item["edge_key"]),
            source_graph_key=str(item["source_graph_key"]),
            target_graph_key=str(item["target_graph_key"]),
            chapter_start=int(item.get("chapter_start") or 0),
            chapter_end=int(item.get("chapter_end") or 0),
            relation_type=str(item.get("relation_type") or ""),
            polarity=str(item.get("polarity") or "neutral"),
            strength=str(item.get("strength") or "medium"),
            directionality=str(item.get("directionality") or ""),
            stability=str(item.get("stability") or ""),
            current_status=str(item.get("current_status") or ""),
            summary=str(item.get("summary") or ""),
            evidence_chapters=[int(value) for value in item.get("evidence_chapters") or [] if int(value) > 0],
        ).to_dict()
        for item in raw.get("history", [])
    ]
    return EdgeDetail(
        edge_key=str(raw["edge_key"]),
        book_id=int(raw["book_id"] or 0),
        source_graph_key=str(raw["source_graph_key"]),
        target_graph_key=str(raw["target_graph_key"]),
        source_name=str(raw.get("source_name") or ""),
        target_name=str(raw.get("target_name") or ""),
        summary=str(raw.get("summary") or ""),
        structural_relation=[str(item or "").strip() for item in raw.get("structural_relation") or [] if str(item or "").strip()],
        action_relation=[str(item or "").strip() for item in raw.get("action_relation") or [] if str(item or "").strip()],
        emotional_relation=[str(item or "").strip() for item in raw.get("emotional_relation") or [] if str(item or "").strip()],
        directionality=str(raw.get("directionality") or ""),
        stability=str(raw.get("stability") or ""),
        current_status=str(raw.get("current_status") or ""),
        drivers=[str(item or "").strip() for item in raw.get("drivers") or [] if str(item or "").strip()],
        history=history,
    ).to_dict()
