from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class GraphNode:
    graph_key: str
    book_id: int
    character_id: int | None
    name: str
    aliases: list[str] = field(default_factory=list)
    profile_status: str = "stub"
    current_state_summary: str = ""
    first_chapter_index: int = 0
    last_chapter_index: int = 0
    version_hash: str = ""
    degree: int = 0
    x: float = 0.0
    y: float = 0.0
    size: float = 18.0
    color: str = "#38BDF8"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GraphEdge:
    edge_key: str
    book_id: int
    source_graph_key: str
    target_graph_key: str
    source_name: str
    target_name: str
    summary: str = ""
    structural_relation: list[str] = field(default_factory=list)
    action_relation: list[str] = field(default_factory=list)
    emotional_relation: list[str] = field(default_factory=list)
    directionality: str = ""
    stability: str = ""
    current_status: str = ""
    drivers: list[str] = field(default_factory=list)
    first_chapter_index: int = 0
    last_chapter_index: int = 0
    version_hash: str = ""
    color: str = "rgba(125,211,252,0.45)"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RelationEvent:
    event_key: str
    book_id: int
    edge_key: str
    source_graph_key: str
    target_graph_key: str
    chapter_start: int
    chapter_end: int
    relation_type: str = ""
    polarity: str = "neutral"
    strength: str = "medium"
    directionality: str = ""
    stability: str = ""
    current_status: str = ""
    summary: str = ""
    evidence_chapters: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NodeDetail:
    graph_key: str
    book_id: int
    character_id: int | None
    name: str
    aliases: list[str] = field(default_factory=list)
    profile_status: str = "stub"
    current_state_summary: str = ""
    first_chapter_index: int = 0
    last_chapter_index: int = 0
    version_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EdgeDetail:
    edge_key: str
    book_id: int
    source_graph_key: str
    target_graph_key: str
    source_name: str
    target_name: str
    summary: str = ""
    structural_relation: list[str] = field(default_factory=list)
    action_relation: list[str] = field(default_factory=list)
    emotional_relation: list[str] = field(default_factory=list)
    directionality: str = ""
    stability: str = ""
    current_status: str = ""
    drivers: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SyncResult:
    status: str
    synced_nodes: int = 0
    synced_edges: int = 0
    synced_events: int = 0
    upgraded_stub_keys: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
