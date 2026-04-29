from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pydantic
import plotly.graph_objects as go

import reflex as rx

from core.public_runtime import (
    build_startup_settings,
    get_runtime_override_path,
    is_agent_runtime_prewarm_enabled,
    probe_startup_services,
    refresh_runtime_clients,
    test_startup_settings,
    validate_startup_settings,
    write_startup_settings,
)

DEFAULT_COVER_URL = "https://placehold.co/150x200"
MAX_CHAT_MESSAGES = 60
CHARACTER_ARCHIVE_PAGE_SIZE = 10
CHARACTER_ARCHIVE_RECORD_THRESHOLD_OPTIONS = (1, 5, 10, 20, 50, 100)
RELATION_GRAPH_EDGE_SAMPLE_POINTS = 21
RELATION_GRAPH_EDGE_HIT_SIZE = 14
RELATION_GRAPH_VIEWBOX_WIDTH = 1000.0
RELATION_GRAPH_VIEWBOX_HEIGHT = 640.0
RELATION_GRAPH_WORLD_WIDTH = 1560.0
RELATION_GRAPH_WORLD_HEIGHT = 1080.0
RELATION_GRAPH_WORLD_PADDING_X = 108.0
RELATION_GRAPH_WORLD_PADDING_Y = 92.0
RELATION_GRAPH_NODE_SPACING = 44.0
RELATION_GRAPH_RELAXATION_PASSES = 28

logger = logging.getLogger(__name__)
_BOOK_STATUS_RECOVERY_DONE = False
_WEB_ENV_PATH = Path(__file__).resolve().parents[1] / ".web" / "env.json"
_PICTURE_DIR = Path(__file__).resolve().parents[1] / "data" / "picture"


def _setup_terminal_logging() -> None:
    level_name = os.getenv("STORY2MEMORY_LOG_LEVEL", "INFO").strip().upper() or "INFO"
    level = getattr(logging, level_name, logging.INFO)
    root_logger = logging.getLogger()

    has_story2memory_console = any(
        getattr(handler, "_story2memory_console", False) for handler in root_logger.handlers
    )
    if not has_story2memory_console:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setLevel(level)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
        setattr(handler, "_story2memory_console", True)
        root_logger.addHandler(handler)

    root_logger.setLevel(level)
    logger.info("Terminal logging enabled. level=%s", logging.getLevelName(level))


_setup_terminal_logging()


def _resolve_backend_origin() -> str:
    try:
        if _WEB_ENV_PATH.exists():
            payload = json.loads(_WEB_ENV_PATH.read_text(encoding="utf-8"))
            for key in ("UPLOAD", "PING", "HEALTH", "ALL_ROUTES"):
                value = str(payload.get(key) or "").strip()
                if not value:
                    continue
                if "://" in value:
                    base = value.split("://", 1)
                    scheme = base[0]
                    host = base[1].split("/", 1)[0]
                    if scheme and host:
                        return f"{scheme}://{host}"
    except Exception:
        logger.exception("Failed to resolve backend origin from .web/env.json.")
    return ""


def _resolve_cover_url(raw_cover: str) -> str:
    cover = str(raw_cover or "").strip()
    if not cover:
        return DEFAULT_COVER_URL
    if cover.startswith(("http://", "https://", "data:")):
        return cover
    if cover.startswith("/covers/"):
        backend_origin = _resolve_backend_origin()
        if backend_origin:
            return f"{backend_origin}{cover}"
    return cover


def _managed_cover_exists(raw_cover: str) -> bool:
    cover = str(raw_cover or "").strip()
    if not cover.startswith("/covers/"):
        return False
    candidate = (_PICTURE_DIR / cover.split("/covers/", 1)[1]).resolve()
    picture_root = _PICTURE_DIR.resolve()
    return candidate.is_file() and (candidate == picture_root or picture_root in candidate.parents)


def _book_cover_fallback(title: str) -> str:
    safe_title = html.escape(str(title or "").strip() or "Untitled")
    short_title = safe_title if len(safe_title) <= 18 else f"{safe_title[:17]}…"
    svg = f"""
<svg xmlns="http://www.w3.org/2000/svg" width="300" height="420" viewBox="0 0 300 420">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#0f172a" />
      <stop offset="100%" stop-color="#1d4ed8" />
    </linearGradient>
  </defs>
  <rect width="300" height="420" rx="24" fill="url(#bg)" />
  <rect x="22" y="22" width="256" height="376" rx="18" fill="none" stroke="rgba(255,255,255,0.24)" />
  <text x="36" y="88" fill="#93c5fd" font-size="18" font-family="Arial, sans-serif">Story2Memory</text>
  <text x="36" y="178" fill="#f8fafc" font-size="30" font-weight="700" font-family="Arial, sans-serif">{short_title}</text>
  <text x="36" y="356" fill="#bfdbfe" font-size="16" font-family="Arial, sans-serif">Cover missing, restored locally</text>
</svg>
""".strip()
    return f"data:image/svg+xml;utf8,{quote(svg)}"


def _resolve_book_cover(raw_cover: str, *, title: str) -> str:
    cover = str(raw_cover or "").strip()
    if not cover:
        return _book_cover_fallback(title)
    if cover.startswith("/covers/") and not _managed_cover_exists(cover):
        return _book_cover_fallback(title)
    return _resolve_cover_url(cover)


class Book(pydantic.BaseModel):
    id: int = 0
    title: str
    meta: str
    cover: str = DEFAULT_COVER_URL
    status: str = "pending"


class ChatMessage(pydantic.BaseModel):
    role: str
    content: str


class ConfirmedEvidence(pydantic.BaseModel):
    subquery_id: str = ""
    label: str = ""
    chapter_index: int = 0
    source_name: str = ""
    claim: str = ""
    excerpt: str = ""
    highlighted_html: str = ""
    confidence: float = 0.0


class CharacterArchiveCard(pydantic.BaseModel):
    id: int = 0
    name: str = ""
    alias_preview: list[str] = []
    alias_preview_text: str = ""
    record_count: int = 0
    first_chapter_index: int = 0
    last_chapter_index: int = 0


class CharacterVolumeArc(pydantic.BaseModel):
    volume_index: int = 0
    volume_title: str = ""
    summary: str = ""
    role_in_volume: list[str] = []
    goals: list[str] = []
    state_changes: list[str] = []
    relationship_changes: list[str] = []


class CharacterRelationHistory(pydantic.BaseModel):
    chapter_start: int = 0
    chapter_end: int = 0
    relation_type: str = ""
    structural_relation: list[str] = []
    action_relation: list[str] = []
    emotional_relation: list[str] = []
    polarity: str = ""
    strength: str = ""
    directionality: str = ""
    stability: str = ""
    current_status: str = ""
    drivers: list[str] = []
    summary: str = ""


class CharacterRelationView(pydantic.BaseModel):
    target_character_name: str = ""
    summary: str = ""
    structural_relation: list[str] = []
    action_relation: list[str] = []
    emotional_relation: list[str] = []
    directionality: str = ""
    stability: str = ""
    current_status: str = ""
    drivers: list[str] = []
    history: list[CharacterRelationHistory] = []


class CharacterEmotionalRelationTimeline(pydantic.BaseModel):
    chapter_start: int = 0
    chapter_end: int = 0
    summary: str = ""


class CharacterEmotionalRelationView(pydantic.BaseModel):
    target_character_name: str = ""
    relation_summary: str = ""
    primary_relation_type: str = ""
    secondary_emotional_tendencies: list[str] = []
    intensity: str = ""
    current_status: str = ""
    timeline: list[CharacterEmotionalRelationTimeline] = []


class CharacterStyleSampleView(pydantic.BaseModel):
    scene: str = ""
    quote: str = ""


class CharacterProfileView(pydantic.BaseModel):
    identity_summary: str = ""
    aliases: list[str] = []
    appearance: list[str] = []
    narrative_role: list[str] = []
    personality_and_style: list[str] = []
    style_summary: str = ""
    speech_style: list[str] = []
    style_samples: list[CharacterStyleSampleView] = []
    goals_and_motivation: list[str] = []
    stance_and_alignment: list[str] = []
    abilities_and_resources: list[str] = []
    stable_profile: list[str] = []
    emotional_relations: list[CharacterEmotionalRelationView] = []
    volume_arc: list[CharacterVolumeArc] = []
    current_state: list[str] = []
    turning_points: list[str] = []
    key_events: list[str] = []


def _dedupe_non_empty_strs(values: list[Any] | None) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in values or []:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        deduped.append(text)
        seen.add(text)
    return deduped


class RelationGraphNodeView(pydantic.BaseModel):
    graph_key: str = ""
    book_id: int = 0
    character_id: int = 0
    name: str = ""
    aliases: list[str] = []
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


class RelationGraphEdgeView(pydantic.BaseModel):
    edge_key: str = ""
    book_id: int = 0
    source_graph_key: str = ""
    target_graph_key: str = ""
    source_name: str = ""
    target_name: str = ""
    summary: str = ""
    structural_relation: list[str] = []
    action_relation: list[str] = []
    emotional_relation: list[str] = []
    directionality: str = ""
    stability: str = ""
    current_status: str = ""
    drivers: list[str] = []
    first_chapter_index: int = 0
    last_chapter_index: int = 0
    version_hash: str = ""
    color: str = "rgba(125,211,252,0.45)"


class RelationGraphEventDetailView(pydantic.BaseModel):
    event_key: str = ""
    chapter_start: int = 0
    chapter_end: int = 0
    relation_type: str = ""
    polarity: str = ""
    strength: str = ""
    directionality: str = ""
    stability: str = ""
    current_status: str = ""
    summary: str = ""
    evidence_chapters: list[int] = []


class RelationGraphNodeDetailView(pydantic.BaseModel):
    graph_key: str = ""
    book_id: int = 0
    character_id: int = 0
    name: str = ""
    aliases: list[str] = []
    profile_status: str = "stub"
    current_state_summary: str = ""
    first_chapter_index: int = 0
    last_chapter_index: int = 0
    version_hash: str = ""


class RelationGraphEdgeDetailView(pydantic.BaseModel):
    edge_key: str = ""
    book_id: int = 0
    source_graph_key: str = ""
    target_graph_key: str = ""
    source_name: str = ""
    target_name: str = ""
    summary: str = ""
    structural_relation: list[str] = []
    action_relation: list[str] = []
    emotional_relation: list[str] = []
    directionality: str = ""
    stability: str = ""
    current_status: str = ""
    drivers: list[str] = []
    first_chapter_index: int = 0
    last_chapter_index: int = 0
    history: list[RelationGraphEventDetailView] = []


class RelationGraphCanvasNodeView(pydantic.BaseModel):
    graph_key: str = ""
    name: str = ""
    x: int = 0
    y: int = 0
    radius: int = 0
    label_y: int = 0
    color: str = "#38BDF8"


class RelationGraphCanvasEdgeView(pydantic.BaseModel):
    edge_key: str = ""
    source_graph_key: str = ""
    target_graph_key: str = ""
    path_d: str = ""
    color: str = "rgba(125,211,252,0.45)"


class StartupServiceStatus(pydantic.BaseModel):
    key: str = ""
    label: str = ""
    status: str = "starting"
    detail: str = ""
    blocking: bool = True


class NovelState(rx.State):
    default_books: list[Book] = []
    uploaded_books: list[Book] = []
    page_mode: str = "startup_setup"
    current_book_id: int = 0
    current_novel: str = ""
    current_character_id: int = 0
    current_character_name: str = ""
    startup_feedback: str = ""
    startup_feedback_is_error: bool = False
    startup_last_saved_at: str = ""
    startup_services_loading: bool = False
    startup_test_running: bool = False
    startup_apply_running: bool = False
    startup_test_passed: bool = False
    startup_last_test_signature: str = ""
    startup_service_statuses: list[StartupServiceStatus] = []
    setup_ark_api_key: str = ""
    setup_llm_model: str = ""
    setup_vector_retrieval_enabled: bool = True
    setup_embed_model: str = ""
    setup_rerank_enabled: bool = True
    setup_rerank_provider: str = "qwen"
    setup_rerank_base_url: str = "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"
    setup_rerank_api_key: str = ""
    setup_rerank_model: str = "qwen3-rerank"
    setup_rerank_instruction: str = "Given a web search query, retrieve relevant passages that answer the query."
    setup_prewarm_enabled: bool = False
    chat_mode: str = "qa"
    chat_input: str = ""
    is_generating: bool = False
    chat_messages: list[ChatMessage] = [
        ChatMessage(
            role="assistant",
            content="你好，我是小说分析助手。你可以先从左侧选择一本小说，然后开始提问。",
        )
    ]
    confirmed_evidence: list[ConfirmedEvidence] = []
    upload_feedback: str = ""
    upload_feedback_is_error: bool = False
    is_uploading: bool = False
    is_analyzing: bool = False
    is_deleting_book: bool = False
    analysis_feedback: str = ""
    analysis_feedback_is_error: bool = False
    analysis_feedback_visible: bool = False
    delete_book_dialog_open: bool = False
    delete_book_target_id: int = 0
    delete_book_target_title: str = ""
    character_archive_items: list[CharacterArchiveCard] = []
    character_archive_page: int = 1
    character_archive_record_threshold: int = 20
    current_character_card: CharacterArchiveCard = CharacterArchiveCard()
    current_character_profile: CharacterProfileView = CharacterProfileView()
    current_character_relations: list[CharacterRelationView] = []
    character_archive_loading: bool = False
    character_detail_loading: bool = False
    is_character_generating: bool = False
    is_character_roleplay_generating: bool = False
    character_feedback: str = ""
    character_feedback_is_error: bool = False
    character_feedback_visible: bool = False
    relation_graph_loading: bool = False
    relation_graph_error: str = ""
    relation_graph_nodes: list[RelationGraphNodeView] = []
    relation_graph_edges: list[RelationGraphEdgeView] = []
    relation_graph_selected_node_key: str = ""
    relation_graph_selected_edge_key: str = ""
    relation_graph_selected_kind: str = ""
    relation_graph_node_detail: RelationGraphNodeDetailView = RelationGraphNodeDetailView()
    relation_graph_edge_detail: RelationGraphEdgeDetailView = RelationGraphEdgeDetailView()

    @rx.var
    def bookshelf_items(self) -> list[Book]:
        return self.uploaded_books

    def _current_book(self) -> Book | None:
        if self.current_book_id > 0:
            for book in self.uploaded_books:
                if int(book.id or 0) == int(self.current_book_id):
                    return book
        active_title = self.current_novel.strip()
        if active_title:
            for book in self.uploaded_books:
                if book.title == active_title:
                    return book
        return None

    @rx.var
    def context_label(self) -> str:
        if self.page_mode == "character_detail" and self.current_character_name.strip():
            return self.current_character_name.strip()
        active_book = self._current_book()
        active_title = str(active_book.title or "").strip() if active_book else ""
        return f"《{active_title}》" if active_title else "全局"

    @rx.var
    def chat_title(self) -> str:
        return "🎭 角色扮演对话" if self.chat_mode == "roleplay" else "🤖 Agent 对话"

    @rx.var
    def roleplay_chat_available(self) -> bool:
        return False

    @rx.var
    def current_book_cover(self) -> str:
        active_book = self._current_book()
        if active_book is None:
            return DEFAULT_COVER_URL
        return str(active_book.cover or DEFAULT_COVER_URL)

    @rx.var
    def character_generate_button_text(self) -> str:
        return "生成中..." if self.is_character_generating else "画像生成"

    @rx.var
    def character_generate_button_disabled(self) -> bool:
        return self.is_character_generating or self.is_character_roleplay_generating or self.current_character_id <= 0

    @rx.var
    def character_roleplay_generate_button_text(self) -> str:
        return "生成中..." if self.is_character_roleplay_generating else "角色扮演加强信息生成"

    @rx.var
    def character_roleplay_generate_button_disabled(self) -> bool:
        return self.is_character_generating or self.is_character_roleplay_generating or self.current_character_id <= 0

    @rx.var
    def character_archive_filtered_items(self) -> list[CharacterArchiveCard]:
        threshold = int(self.character_archive_record_threshold or 20)
        return [
            item
            for item in self.character_archive_items
            if int(item.record_count or 0) > threshold
        ]

    @rx.var
    def character_archive_has_visible_items(self) -> bool:
        return len(self.character_archive_filtered_items) > 0

    @rx.var
    def character_archive_total_pages(self) -> int:
        total_items = len(self.character_archive_filtered_items)
        if total_items <= 0:
            return 1
        return max(1, (total_items + CHARACTER_ARCHIVE_PAGE_SIZE - 1) // CHARACTER_ARCHIVE_PAGE_SIZE)

    @rx.var
    def character_archive_page_items(self) -> list[CharacterArchiveCard]:
        if not self.character_archive_filtered_items:
            return []
        current_page = max(1, min(int(self.character_archive_page or 1), int(self.character_archive_total_pages or 1)))
        start = (current_page - 1) * CHARACTER_ARCHIVE_PAGE_SIZE
        end = start + CHARACTER_ARCHIVE_PAGE_SIZE
        return self.character_archive_filtered_items[start:end]

    @rx.var
    def character_archive_page_label(self) -> str:
        return f"第 {max(1, int(self.character_archive_page or 1))} / {max(1, int(self.character_archive_total_pages or 1))} 页"

    @rx.var
    def character_archive_prev_disabled(self) -> bool:
        return int(self.character_archive_page or 1) <= 1

    @rx.var
    def character_archive_next_disabled(self) -> bool:
        return int(self.character_archive_page or 1) >= int(self.character_archive_total_pages or 1)

    @rx.var
    def startup_validation_errors(self) -> list[str]:
        return validate_startup_settings(self._startup_settings_payload())

    @rx.var
    def startup_test_is_fresh(self) -> bool:
        return bool(self.startup_test_passed) and (
            self.startup_last_test_signature == self._startup_settings_signature()
        )

    @rx.var
    def startup_has_blocking_service_failures(self) -> bool:
        for item in self.startup_service_statuses:
            if not self._startup_service_is_blocking(item):
                continue
            if str(item.status or "starting") != "ready":
                return True
        return False

    @rx.var
    def startup_status_label(self) -> str:
        if self.startup_validation_errors:
            return "待补全 Ark 配置"
        if self.startup_apply_running:
            return "正在应用配置"
        if self.startup_test_running:
            return "正在测试"
        if self.startup_test_is_fresh:
            return "可进入书架"
        if self.startup_has_blocking_service_failures:
            return "等待服务启动"
        return "待测试"

    @rx.var
    def startup_status_accent(self) -> str:
        if self.startup_validation_errors:
            return "#fda4af"
        if self.startup_has_blocking_service_failures:
            return "#fbbf24"
        if self.startup_test_is_fresh:
            return "#86efac"
        if self.startup_apply_running or self.startup_test_running:
            return "#67e8f9"
        return "#cbd5e1"

    @rx.var
    def startup_refresh_button_text(self) -> str:
        return "刷新中..." if self.startup_services_loading else "刷新状态"

    @rx.var
    def startup_test_button_text(self) -> str:
        return "测试中..." if self.startup_test_running else "测试配置"

    @rx.var
    def startup_begin_button_text(self) -> str:
        return "应用中..." if self.startup_apply_running else "开始使用"

    @rx.var
    def startup_test_button_disabled(self) -> bool:
        return (
            self.startup_services_loading
            or self.startup_test_running
            or self.startup_apply_running
            or bool(self.startup_validation_errors)
            or self.startup_has_blocking_service_failures
        )

    @rx.var
    def startup_begin_button_disabled(self) -> bool:
        return (
            self.startup_services_loading
            or self.startup_test_running
            or self.startup_apply_running
            or (not self.startup_test_is_fresh)
            or self.startup_has_blocking_service_failures
        )

    @rx.var
    def startup_runtime_path_label(self) -> str:
        return str(get_runtime_override_path())

    def _startup_settings_payload(self) -> dict[str, Any]:
        return {
            "ark_api_key": self.setup_ark_api_key,
            "llm_model": self.setup_llm_model,
            "vector_retrieval_enabled": True,
            "embed_model": self.setup_embed_model,
            "rerank_enabled": True,
            "rerank_provider": self.setup_rerank_provider,
            "rerank_base_url": self.setup_rerank_base_url,
            "rerank_api_key": self.setup_rerank_api_key,
            "rerank_model": self.setup_rerank_model,
            "rerank_instruction": self.setup_rerank_instruction,
            "prewarm_enabled": bool(self.setup_prewarm_enabled),
        }

    def _startup_settings_signature(self) -> str:
        return json.dumps(self._startup_settings_payload(), ensure_ascii=False, sort_keys=True)

    def _startup_service_is_blocking(self, item: StartupServiceStatus) -> bool:
        return bool(item.blocking)

    def _coerce_startup_service_statuses(self, items: list[dict[str, Any]]) -> list[StartupServiceStatus]:
        return [StartupServiceStatus(**item) for item in items]

    def _apply_optional_service_overrides(self) -> None:
        self.startup_service_statuses = list(self.startup_service_statuses)

    def _invalidate_startup_test_state(self) -> None:
        self.startup_test_passed = False
        self.startup_last_test_signature = ""

    def _load_startup_service_statuses(self) -> None:
        statuses = probe_startup_services(self._startup_settings_payload())
        self.startup_service_statuses = self._coerce_startup_service_statuses(statuses)
        self._apply_optional_service_overrides()

    def load_startup_settings(self):
        settings = build_startup_settings()
        self.setup_ark_api_key = str(settings.get("ark_api_key", "") or "")
        self.setup_llm_model = str(settings.get("llm_model", "") or "")
        self.setup_vector_retrieval_enabled = True
        self.setup_embed_model = str(settings.get("embed_model", "") or "")
        self.setup_rerank_enabled = True
        self.setup_rerank_provider = str(settings.get("rerank_provider", "qwen") or "qwen")
        self.setup_rerank_base_url = str(
            settings.get("rerank_base_url", "https://dashscope.aliyuncs.com/compatible-api/v1/reranks")
            or "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"
        )
        self.setup_rerank_api_key = str(settings.get("rerank_api_key", "") or "")
        self.setup_rerank_model = str(settings.get("rerank_model", "qwen3-rerank") or "qwen3-rerank")
        self.setup_rerank_instruction = str(
            settings.get(
                "rerank_instruction",
                "Given a web search query, retrieve relevant passages that answer the query.",
            )
            or ""
        )
        self.setup_prewarm_enabled = bool(settings.get("prewarm_enabled", False))
        self._invalidate_startup_test_state()
        self.startup_apply_running = False
        self.startup_test_running = False

    def initialize_app(self):
        self.load_books()
        self.load_startup_settings()
        self._load_startup_service_statuses()
        self.page_mode = "startup_setup"

    def enter_bookshelf(self):
        if self.page_mode == "startup_setup" and not self.startup_test_is_fresh:
            self.startup_feedback = "请先完成 Ark 配置测试，再进入书架。"
            self.startup_feedback_is_error = True
            return
        self.page_mode = "bookshelf"
        self.load_books()

    def save_startup_config(self):
        target = write_startup_settings(self._startup_settings_payload())
        self.startup_last_saved_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.startup_feedback = f"配置已保存到 {target}。"
        self.startup_feedback_is_error = False

    async def refresh_startup_service_statuses(self):
        self.startup_services_loading = True
        self.startup_feedback = ""
        self.startup_feedback_is_error = False
        yield
        try:
            statuses = await asyncio.to_thread(
                probe_startup_services,
                self._startup_settings_payload(),
            )
            self.startup_service_statuses = self._coerce_startup_service_statuses(statuses)
            self._apply_optional_service_overrides()
        finally:
            self.startup_services_loading = False

    async def test_startup_config(self):
        errors = validate_startup_settings(self._startup_settings_payload())
        if errors:
            self.startup_feedback = "配置未完成：\n" + "\n".join(errors)
            self.startup_feedback_is_error = True
            self._invalidate_startup_test_state()
            return
        if self.startup_has_blocking_service_failures:
            self.startup_feedback = "服务未启动：请先等待 Docker 依赖服务就绪，再进行 Ark 配置测试。"
            self.startup_feedback_is_error = True
            self._invalidate_startup_test_state()
            return

        self.startup_test_running = True
        self.startup_feedback = ""
        self.startup_feedback_is_error = False
        yield
        try:
            result = await asyncio.to_thread(
                test_startup_settings,
                self._startup_settings_payload(),
            )
            self.startup_service_statuses = self._coerce_startup_service_statuses(
                list(result.get("services", []))
            )
            self._apply_optional_service_overrides()
            if bool(result.get("ok")):
                self.startup_test_passed = True
                self.startup_last_test_signature = self._startup_settings_signature()
                self.startup_feedback = str(result.get("message", "") or "Ark 配置测试通过。")
                self.startup_feedback_is_error = False
            else:
                self._invalidate_startup_test_state()
                group = str(result.get("group", "") or "").strip()
                message = str(result.get("message", "") or "Ark 配置测试失败。").strip()
                self.startup_feedback = f"{group}：{message}" if group else message
                self.startup_feedback_is_error = True
        finally:
            self.startup_test_running = False

    async def apply_startup_config(self):
        errors = validate_startup_settings(self._startup_settings_payload())
        if errors:
            self.startup_feedback = "配置未完成：\n" + "\n".join(errors)
            self.startup_feedback_is_error = True
            return
        if not self.startup_test_is_fresh:
            self.startup_feedback = "请先完成 Ark 配置测试，并保持当前输入未变更。"
            self.startup_feedback_is_error = True
            return
        if self.startup_has_blocking_service_failures:
            self.startup_feedback = "依赖服务尚未就绪，当前不能进入应用。"
            self.startup_feedback_is_error = True
            return

        self.startup_apply_running = True
        self.startup_feedback = "正在应用 Ark 配置并刷新运行时客户端..."
        self.startup_feedback_is_error = False
        yield
        try:
            target = await asyncio.to_thread(
                write_startup_settings,
                self._startup_settings_payload(),
            )
            await asyncio.to_thread(
                refresh_runtime_clients,
                self._startup_settings_payload(),
            )
            self.startup_last_saved_at = time.strftime("%Y-%m-%d %H:%M:%S")
            self.startup_feedback = f"配置已写入 {target}，当前会话已切换到新的 Ark 运行时。"
            self.startup_feedback_is_error = False
            self.load_startup_settings()
            self._load_startup_service_statuses()
            self.current_book_id = 0
            self.current_novel = ""
            self.current_character_id = 0
            self.current_character_name = ""
            self.chat_mode = "qa"
            self.chat_messages = self._default_chat_messages()
            self.chat_input = ""
            self.page_mode = "bookshelf"
            self.load_books()
        except Exception as exc:
            logger.exception("Failed to apply startup config.")
            self.startup_feedback = f"应用配置失败：{exc}"
            self.startup_feedback_is_error = True
        finally:
            self.startup_apply_running = False

    def open_startup_setup(self):
        self.load_startup_settings()
        self._load_startup_service_statuses()
        self.page_mode = "startup_setup"

    def set_setup_ark_api_key(self, value: str):
        self.setup_ark_api_key = value
        self._invalidate_startup_test_state()

    def set_setup_llm_model(self, value: str):
        self.setup_llm_model = value
        self._invalidate_startup_test_state()

    def set_setup_vector_retrieval_enabled(self, value: bool):
        self.setup_vector_retrieval_enabled = True
        self._apply_optional_service_overrides()
        self._invalidate_startup_test_state()

    def set_setup_embed_model(self, value: str):
        self.setup_embed_model = value
        self._invalidate_startup_test_state()

    def set_setup_rerank_enabled(self, value: bool):
        self.setup_rerank_enabled = True
        self._apply_optional_service_overrides()
        self._invalidate_startup_test_state()

    def set_setup_rerank_provider(self, value: str):
        self.setup_rerank_provider = value
        self._apply_optional_service_overrides()
        self._invalidate_startup_test_state()

    def set_setup_rerank_base_url(self, value: str):
        self.setup_rerank_base_url = value
        self._invalidate_startup_test_state()

    def set_setup_rerank_api_key(self, value: str):
        self.setup_rerank_api_key = value
        self._invalidate_startup_test_state()

    def set_setup_rerank_model(self, value: str):
        self.setup_rerank_model = value
        self._invalidate_startup_test_state()

    def set_setup_rerank_instruction(self, value: str):
        self.setup_rerank_instruction = value
        self._invalidate_startup_test_state()

    def set_setup_prewarm_enabled(self, value: bool):
        self.setup_prewarm_enabled = bool(value)
        self._invalidate_startup_test_state()

    def set_character_archive_record_threshold(self, value: str):
        try:
            threshold = int(str(value or "").strip())
        except ValueError:
            threshold = 20
        if threshold not in CHARACTER_ARCHIVE_RECORD_THRESHOLD_OPTIONS:
            threshold = 20
        self.character_archive_record_threshold = threshold
        self.character_archive_page = 1

    def open_book(self, book_id: int, title: str):
        self.current_book_id = int(book_id or 0)
        self.current_novel = title
        self.current_character_id = 0
        self.current_character_name = ""
        self.chat_mode = "qa"
        self.chat_messages = self._default_chat_messages()
        self.chat_input = ""
        self.character_archive_items = []
        self.character_archive_page = 1
        self.character_archive_record_threshold = 20
        self.current_character_card = CharacterArchiveCard()
        self.current_character_profile = CharacterProfileView()
        self.current_character_relations = []
        self.is_character_generating = False
        self.is_character_roleplay_generating = False
        self.character_feedback = ""
        self.character_feedback_is_error = False
        self.character_feedback_visible = False
        self._reset_relation_graph_state()
        self.page_mode = "detail"

    def prompt_delete_book(self, book_id: int, title: str):
        self.delete_book_target_id = int(book_id or 0)
        self.delete_book_target_title = str(title or "").strip()
        self.delete_book_dialog_open = self.delete_book_target_id > 0

    def cancel_delete_book(self):
        self.delete_book_dialog_open = False
        self.delete_book_target_id = 0
        self.delete_book_target_title = ""
        self.is_deleting_book = False

    async def confirm_delete_book(self):
        book_id = int(self.delete_book_target_id or 0)
        title = str(self.delete_book_target_title or "").strip()
        if book_id <= 0 or self.is_deleting_book:
            return

        self.is_deleting_book = True
        self.upload_feedback = ""
        self.upload_feedback_is_error = False
        yield

        try:
            from rag.uploadBook import delete_book_cascade

            result = await asyncio.to_thread(delete_book_cascade, book_id)
            if int(result.get("deleted") or 0) <= 0:
                raise RuntimeError("未找到要删除的书籍记录。")
            if int(self.current_book_id or 0) == book_id:
                self.current_book_id = 0
                self.current_novel = ""
                self.current_character_id = 0
                self.current_character_name = ""
                self.chat_mode = "qa"
                self.chat_messages = self._default_chat_messages()
                self.chat_input = ""
                self.character_archive_items = []
                self.character_archive_page = 1
                self.character_archive_record_threshold = 20
                self.current_character_card = CharacterArchiveCard()
                self.current_character_profile = CharacterProfileView()
                self.current_character_relations = []
                self._reset_relation_graph_state()
            self.page_mode = "bookshelf"
            self.load_books()
            self.upload_feedback = f"已删除《{title or f'书籍#{book_id}'}》及其相关分析与对话。"
            self.upload_feedback_is_error = False
        except Exception as exc:
            logger.exception("Failed to delete book: book_id=%s", book_id)
            self.upload_feedback = f"删除失败：{exc}"
            self.upload_feedback_is_error = True
        finally:
            self.delete_book_dialog_open = False
            self.delete_book_target_id = 0
            self.delete_book_target_title = ""
            self.is_deleting_book = False
            yield

    def back_to_bookshelf(self):
        self.page_mode = "bookshelf"
        self.current_book_id = 0
        self.current_novel = ""
        self.current_character_id = 0
        self.current_character_name = ""
        self.chat_mode = "qa"
        self.chat_messages = self._default_chat_messages()
        self.chat_input = ""
        self.character_archive_items = []
        self.character_archive_page = 1
        self.character_archive_record_threshold = 20
        self.current_character_card = CharacterArchiveCard()
        self.current_character_profile = CharacterProfileView()
        self.current_character_relations = []
        self.is_character_generating = False
        self.is_character_roleplay_generating = False
        self._reset_relation_graph_state()
        self.load_books()

    def back_to_book_detail(self):
        self.page_mode = "detail"
        self.current_character_id = 0
        self.current_character_name = ""
        self.chat_mode = "qa"
        self.chat_messages = self._default_chat_messages()
        self.chat_input = ""
        self.current_character_card = CharacterArchiveCard()
        self.current_character_profile = CharacterProfileView()
        self.current_character_relations = []
        self.is_character_generating = False
        self.is_character_roleplay_generating = False
        self.character_feedback = ""
        self.character_feedback_is_error = False
        self.character_feedback_visible = False
        self._reset_relation_graph_state()

    def character_archive_prev_page(self):
        self.character_archive_page = max(1, int(self.character_archive_page or 1) - 1)

    def character_archive_next_page(self):
        self.character_archive_page = min(
            int(self.character_archive_total_pages or 1),
            int(self.character_archive_page or 1) + 1,
        )

    def _reset_character_feedback(self):
        self.is_character_generating = False
        self.is_character_roleplay_generating = False
        self.character_feedback = ""
        self.character_feedback_is_error = False
        self.character_feedback_visible = False

    def _default_chat_messages(self) -> list[ChatMessage]:
        return [
            ChatMessage(
                role="assistant",
                content="你好，我是小说分析助手。你可以先从左侧选择一本小说，然后开始提问。",
            )
        ]

    def _roleplay_chat_messages(self) -> list[ChatMessage]:
        character_name = self.current_character_name.strip() or "该角色"
        novel_title = self.current_novel.strip() or "当前作品"
        return [
            ChatMessage(
                role="assistant",
                content=f"现在进入《{novel_title}》中《{character_name}》的角色扮演对话。你可以直接和我说话。",
            )
        ]

    def _roleplay_loading_chat_messages(self) -> list[ChatMessage]:
        return [
            ChatMessage(
                role="assistant",
                content="正在载入角色扮演上下文...",
            )
        ]

    def enter_roleplay_chat(self):
        if self.chat_mode == "roleplay":
            return
        if self.current_character_id <= 0 or not self.current_character_name.strip():
            return
        self.chat_mode = "roleplay"
        self.chat_messages = self._roleplay_chat_messages()
        self.chat_input = ""

    def exit_roleplay_chat(self):
        if self.chat_mode == "qa":
            return
        self.chat_mode = "qa"
        self.chat_messages = self._default_chat_messages()
        self.chat_input = ""

    def _build_roleplay_context_payload(self) -> dict[str, Any]:
        profile = self.current_character_profile
        relation_lines = []
        for item in profile.emotional_relations[:5]:
            if not item.target_character_name:
                continue
            if item.primary_relation_type == "爱慕/暧昧/恋爱":
                qualifiers = "，".join(item.secondary_emotional_tendencies[:3]) if item.secondary_emotional_tendencies else "特殊关注"
                relation_lines.append(
                    f"{item.target_character_name}：与其存在{qualifiers}，但未明确确认私人关系。"
                )
                continue
            if item.relation_summary:
                relation_lines.append(f"{item.target_character_name}：{item.relation_summary}")
                continue
            if item.secondary_emotional_tendencies:
                relation_lines.append(f"{item.target_character_name}：{'，'.join(item.secondary_emotional_tendencies)}")
                continue
            if item.current_status:
                relation_lines.append(f"{item.target_character_name}：{item.current_status}")
        current_state = "；".join([str(item or "").strip() for item in profile.current_state if str(item or "").strip()][:4]) or "暂无"
        speech_style = "；".join([str(item or "").strip() for item in profile.speech_style if str(item or "").strip()][:4]) or "暂无"
        style_sample_lines = [
            f"{item.scene}：{item.quote}"
            for item in profile.style_samples[:4]
            if item.scene and item.quote
        ]
        persona_summary = (
            f"角色名：{self.current_character_name.strip() or '未知'}\n"
            f"身份概览：{profile.identity_summary or '暂无'}\n"
            f"叙事定位：{'；'.join(profile.narrative_role[:3]) if profile.narrative_role else '暂无'}\n"
            f"当前状态：{current_state}\n"
            f"语言风格：{speech_style}\n"
            f"语言风格实例：{'；'.join(style_sample_lines) if style_sample_lines else '暂无'}\n"
            f"关键情感关系：{'；'.join(relation_lines) if relation_lines else '暂无'}"
        )
        relations_payload = [
            item.model_dump() if hasattr(item, "model_dump") else item.dict()
            for item in self.current_character_relations
        ]
        emotional_payload = [
            item.model_dump() if hasattr(item, "model_dump") else item.dict()
            for item in profile.emotional_relations
        ]
        style_samples_payload = [
            item.model_dump() if hasattr(item, "model_dump") else item.dict()
            for item in profile.style_samples
        ]
        return {
            "book_id": int(self.current_book_id or 0),
            "character_id": int(self.current_character_id or 0),
            "character_name": self.current_character_name.strip(),
            "novel_title": self.current_novel.strip(),
            "persona_summary": persona_summary,
            "relations": relations_payload,
            "emotional_relations": emotional_payload,
            "style_samples": style_samples_payload,
        }

    def _reset_relation_graph_state(self):
        self.relation_graph_loading = False
        self.relation_graph_error = ""
        self.relation_graph_nodes = []
        self.relation_graph_edges = []
        self.clear_relation_graph_selection()

    def set_chat_input(self, value: str):
        self.chat_input = value

    def _current_book_status_code(self) -> str:
        active_book = self._current_book()
        if active_book is None:
            return "pending"
        return str(active_book.status or "pending")

    @rx.var
    def current_book_status_text(self) -> str:
        status_map = {
            "pending": "待分析",
            "processing": "分析中",
            "completed": "已分析",
            "error": "分析失败",
        }
        status_code = "processing" if self.is_analyzing else self._current_book_status_code()
        return f"作品状态：{status_map.get(status_code, '待分析')}"

    @rx.var
    def analyze_button_text(self) -> str:
        status_code = "processing" if self.is_analyzing else self._current_book_status_code()
        if status_code == "processing":
            return "分析中..."
        if status_code == "completed":
            return "再次分析"
        return "开始分析"

    @rx.var
    def analyze_button_disabled(self) -> bool:
        status_code = "processing" if self.is_analyzing else self._current_book_status_code()
        return status_code == "processing"

    @rx.var
    def upload_button_text(self) -> str:
        return "上传中" if self.is_uploading else "上传并加入书架"

    @rx.var
    def upload_button_disabled(self) -> bool:
        return self.is_uploading

    @rx.var
    def relation_graph_has_data(self) -> bool:
        return bool(self.relation_graph_nodes)

    @rx.var
    def relation_graph_empty(self) -> bool:
        return (not self.relation_graph_loading) and (not self.relation_graph_error) and (not self.relation_graph_nodes)

    @rx.var
    def relation_graph_node_alias_text(self) -> str:
        return " / ".join(self.relation_graph_node_detail.aliases)

    @rx.var
    def relation_graph_node_chapter_range(self) -> str:
        start = int(self.relation_graph_node_detail.first_chapter_index or 0)
        end = int(self.relation_graph_node_detail.last_chapter_index or 0)
        if start > 0 and end > 0:
            return f"Ch.{start}-{end}"
        return "暂无章节范围"

    @rx.var
    def relation_graph_edge_chapter_range(self) -> str:
        if not self.relation_graph_edge_detail.history:
            start = int(self.relation_graph_edge_detail.first_chapter_index or 0)
            end = int(self.relation_graph_edge_detail.last_chapter_index or 0)
            if start > 0 and end > 0:
                return f"Ch.{start}-{end}"
            return "暂无章节范围"
        start = int(self.relation_graph_edge_detail.history[0].chapter_start or 0)
        end = int(self.relation_graph_edge_detail.history[-1].chapter_end or 0)
        if start > 0 and end > 0:
            return f"Ch.{start}-{end}"
        return "暂无章节范围"

    @staticmethod
    def _sample_relation_graph_curve(
        source_x: float,
        source_y: float,
        control_x: float,
        control_y: float,
        target_x: float,
        target_y: float,
    ) -> tuple[list[float], list[float]]:
        xs: list[float] = []
        ys: list[float] = []
        sample_count = max(3, RELATION_GRAPH_EDGE_SAMPLE_POINTS)
        for index in range(sample_count):
            t = index / float(sample_count - 1)
            inv = 1.0 - t
            xs.append((inv * inv * source_x) + (2.0 * inv * t * control_x) + (t * t * target_x))
            ys.append((inv * inv * source_y) + (2.0 * inv * t * control_y) + (t * t * target_y))
        return xs, ys

    @staticmethod
    def _relation_graph_node_detail_from_view(item: RelationGraphNodeView | None) -> RelationGraphNodeDetailView:
        if item is None:
            return RelationGraphNodeDetailView()
        return RelationGraphNodeDetailView(
            graph_key=item.graph_key,
            book_id=item.book_id,
            character_id=item.character_id,
            name=item.name,
            aliases=list(item.aliases),
            profile_status=item.profile_status,
            current_state_summary=item.current_state_summary,
            first_chapter_index=item.first_chapter_index,
            last_chapter_index=item.last_chapter_index,
            version_hash=item.version_hash,
        )

    @staticmethod
    def _relation_graph_edge_detail_from_view(item: RelationGraphEdgeView | None) -> RelationGraphEdgeDetailView:
        if item is None:
            return RelationGraphEdgeDetailView()
        return RelationGraphEdgeDetailView(
            edge_key=item.edge_key,
            book_id=item.book_id,
            source_graph_key=item.source_graph_key,
            target_graph_key=item.target_graph_key,
            source_name=item.source_name,
            target_name=item.target_name,
            summary=item.summary,
            structural_relation=list(item.structural_relation),
            action_relation=list(item.action_relation),
            emotional_relation=list(item.emotional_relation),
            directionality=item.directionality,
            stability=item.stability,
            current_status=item.current_status,
            drivers=list(item.drivers),
            first_chapter_index=item.first_chapter_index,
            last_chapter_index=item.last_chapter_index,
        )

    def _find_relation_graph_node(self, graph_key: str) -> RelationGraphNodeView | None:
        target_key = str(graph_key or "").strip()
        for item in self.relation_graph_nodes:
            if item.graph_key == target_key:
                return item
        return None

    def _find_relation_graph_edge(self, edge_key: str) -> RelationGraphEdgeView | None:
        target_key = str(edge_key or "").strip()
        for item in self.relation_graph_edges:
            if item.edge_key == target_key:
                return item
        return None

    def _build_relation_graph_canvas_nodes(self) -> list[RelationGraphCanvasNodeView]:
        if not self.relation_graph_nodes:
            return []

        xs = [float(item.x) for item in self.relation_graph_nodes]
        ys = [float(item.y) for item in self.relation_graph_nodes]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        span_x = max(max_x - min_x, 1e-6)
        span_y = max(max_y - min_y, 1e-6)
        usable_width = RELATION_GRAPH_WORLD_WIDTH - (RELATION_GRAPH_WORLD_PADDING_X * 2.0)
        usable_height = RELATION_GRAPH_WORLD_HEIGHT - (RELATION_GRAPH_WORLD_PADDING_Y * 2.0)

        anchors: list[dict[str, float | str | int]] = []
        for item in self.relation_graph_nodes:
            x = RELATION_GRAPH_WORLD_PADDING_X + (((float(item.x) - min_x) / span_x) * usable_width)
            y = RELATION_GRAPH_WORLD_PADDING_Y + (((float(item.y) - min_y) / span_y) * usable_height)
            radius = max(14.0, min(28.0, float(item.size) * 0.58))
            anchors.append(
                {
                    "graph_key": item.graph_key,
                    "name": item.name,
                    "anchor_x": float(x),
                    "anchor_y": float(y),
                    "x": float(x),
                    "y": float(y),
                    "radius": int(round(radius)),
                    "color": item.color,
                }
            )

        for i in range(RELATION_GRAPH_RELAXATION_PASSES):
            for left_index in range(len(anchors)):
                left = anchors[left_index]
                for right_index in range(left_index + 1, len(anchors)):
                    right = anchors[right_index]
                    dx = float(right["x"]) - float(left["x"])
                    dy = float(right["y"]) - float(left["y"])
                    distance = (dx * dx + dy * dy) ** 0.5
                    min_distance = float(left["radius"]) + float(right["radius"]) + RELATION_GRAPH_NODE_SPACING
                    if distance >= min_distance:
                        continue
                    if distance <= 1e-6:
                        dx = 1.0 if (right_index + i) % 2 == 0 else -1.0
                        dy = 1.0 if (left_index + i) % 2 == 0 else -1.0
                        distance = (dx * dx + dy * dy) ** 0.5
                    force = (min_distance - distance) * 0.5
                    unit_x = dx / distance
                    unit_y = dy / distance
                    left["x"] = float(left["x"]) - (unit_x * force)
                    left["y"] = float(left["y"]) - (unit_y * force)
                    right["x"] = float(right["x"]) + (unit_x * force)
                    right["y"] = float(right["y"]) + (unit_y * force)

            for item in anchors:
                item["x"] = (float(item["x"]) * 0.88) + (float(item["anchor_x"]) * 0.12)
                item["y"] = (float(item["y"]) * 0.88) + (float(item["anchor_y"]) * 0.12)
                item["x"] = min(
                    RELATION_GRAPH_WORLD_WIDTH - RELATION_GRAPH_WORLD_PADDING_X,
                    max(RELATION_GRAPH_WORLD_PADDING_X, float(item["x"])),
                )
                item["y"] = min(
                    RELATION_GRAPH_WORLD_HEIGHT - RELATION_GRAPH_WORLD_PADDING_Y,
                    max(RELATION_GRAPH_WORLD_PADDING_Y, float(item["y"])),
                )

        return [
            RelationGraphCanvasNodeView(
                graph_key=str(item["graph_key"]),
                name=str(item["name"]),
                x=int(round(float(item["x"]))),
                y=int(round(float(item["y"]))),
                radius=int(item["radius"]),
                label_y=int(round(max(26.0, float(item["y"]) - int(item["radius"]) - 16.0))),
                color=str(item["color"]),
            )
            for item in anchors
        ]

    def _build_relation_graph_canvas_edges(
        self, canvas_nodes: list[RelationGraphCanvasNodeView]
    ) -> list[RelationGraphCanvasEdgeView]:
        if not canvas_nodes or not self.relation_graph_edges:
            return []

        node_lookup = {item.graph_key: item for item in canvas_nodes}
        pair_size_map: dict[tuple[str, str], int] = {}
        pair_index_map: dict[tuple[str, str], int] = {}
        for edge in self.relation_graph_edges:
            pair = tuple(sorted([edge.source_graph_key, edge.target_graph_key]))
            pair_size_map[pair] = pair_size_map.get(pair, 0) + 1

        canvas_edges: list[RelationGraphCanvasEdgeView] = []
        for edge in self.relation_graph_edges:
            source = node_lookup.get(edge.source_graph_key)
            target = node_lookup.get(edge.target_graph_key)
            if source is None or target is None:
                continue

            pair = tuple(sorted([edge.source_graph_key, edge.target_graph_key]))
            index = pair_index_map.get(pair, 0)
            pair_index_map[pair] = index + 1
            sibling_count = pair_size_map.get(pair, 1)
            direction_sign = 1 if edge.source_graph_key <= edge.target_graph_key else -1

            mid_x = (source.x + target.x) / 2.0
            mid_y = (source.y + target.y) / 2.0
            dx = float(target.x - source.x)
            dy = float(target.y - source.y)
            length = (dx * dx + dy * dy) ** 0.5 or 1.0
            norm_x = -dy / length
            norm_y = dx / length
            offset = 0.0
            if sibling_count > 1:
                offset = ((index - ((sibling_count - 1) / 2.0)) * 36.0) * direction_sign
            control_x = mid_x + (norm_x * offset)
            control_y = mid_y + (norm_y * offset)

            canvas_edges.append(
                RelationGraphCanvasEdgeView(
                    edge_key=edge.edge_key,
                    source_graph_key=edge.source_graph_key,
                    target_graph_key=edge.target_graph_key,
                    path_d=(
                        f"M {source.x:.2f} {source.y:.2f} "
                        f"Q {control_x:.2f} {control_y:.2f} "
                        f"{target.x:.2f} {target.y:.2f}"
                    ),
                    color=edge.color,
                )
            )
        return canvas_edges

    @rx.var
    def relation_graph_canvas_nodes(self) -> list[RelationGraphCanvasNodeView]:
        return self._build_relation_graph_canvas_nodes()

    @rx.var
    def relation_graph_canvas_edges(self) -> list[RelationGraphCanvasEdgeView]:
        return self._build_relation_graph_canvas_edges(self._build_relation_graph_canvas_nodes())

    @rx.var
    def relation_graph_highlighted_edge_keys(self) -> list[str]:
        if self.relation_graph_selected_kind == "node" and self.relation_graph_selected_node_key:
            target_key = str(self.relation_graph_selected_node_key)
            return [
                item.edge_key
                for item in self.relation_graph_edges
                if item.source_graph_key == target_key or item.target_graph_key == target_key
            ]
        if self.relation_graph_selected_kind == "edge" and self.relation_graph_selected_edge_key:
            return [str(self.relation_graph_selected_edge_key)]
        return []

    @rx.var
    def relation_graph_highlighted_node_keys(self) -> list[str]:
        if self.relation_graph_selected_kind == "edge" and self.relation_graph_selected_edge_key:
            edge = self._find_relation_graph_edge(self.relation_graph_selected_edge_key)
            if edge is None:
                return []
            return [edge.source_graph_key, edge.target_graph_key]
        if self.relation_graph_selected_kind == "node" and self.relation_graph_selected_node_key:
            return [str(self.relation_graph_selected_node_key)]
        return []

    @rx.var
    def relation_graph_clickable_edge_keys(self) -> list[str]:
        if self.relation_graph_selected_kind == "node" and self.relation_graph_selected_node_key:
            return self.relation_graph_highlighted_edge_keys
        return [item.edge_key for item in self.relation_graph_edges]

    @rx.var
    def relation_graph_plot_figure(self) -> go.Figure:
        if not self.relation_graph_nodes:
            figure = go.Figure()
            figure.update_layout(
                paper_bgcolor="rgba(0,0,0,0)",
                plot_bgcolor="rgba(0,0,0,0)",
                margin={"l": 0, "r": 0, "t": 0, "b": 0},
                xaxis={"visible": False},
                yaxis={"visible": False},
            )
            return figure

        node_lookup = {item.graph_key: item for item in self.relation_graph_nodes}
        pair_size_map: dict[tuple[str, str], int] = {}
        pair_index_map: dict[tuple[str, str], int] = {}
        for edge in self.relation_graph_edges:
            pair = tuple(sorted([edge.source_graph_key, edge.target_graph_key]))
            pair_size_map[pair] = pair_size_map.get(pair, 0) + 1

        figure = go.Figure()
        for edge in self.relation_graph_edges:
            source = node_lookup.get(edge.source_graph_key)
            target = node_lookup.get(edge.target_graph_key)
            if source is None or target is None:
                continue
            pair = tuple(sorted([edge.source_graph_key, edge.target_graph_key]))
            index = pair_index_map.get(pair, 0)
            pair_index_map[pair] = index + 1
            sibling_count = pair_size_map.get(pair, 1)
            direction_sign = 1 if edge.source_graph_key <= edge.target_graph_key else -1
            offset = 0.0
            if sibling_count > 1:
                offset = ((index - (sibling_count - 1) / 2) * 0.12) * direction_sign
            mid_x = (source.x + target.x) / 2
            mid_y = (source.y + target.y) / 2
            dx = float(target.x - source.x)
            dy = float(target.y - source.y)
            length = (dx * dx + dy * dy) ** 0.5 or 1.0
            norm_x = -dy / length
            norm_y = dx / length
            control_x = mid_x + norm_x * offset
            control_y = mid_y + norm_y * offset
            edge_x, edge_y = self._sample_relation_graph_curve(
                float(source.x),
                float(source.y),
                float(control_x),
                float(control_y),
                float(target.x),
                float(target.y),
            )
            figure.add_trace(
                go.Scatter(
                    x=edge_x,
                    y=edge_y,
                    mode="lines+markers",
                    line={"color": edge.color, "width": 2},
                    marker={
                        "size": RELATION_GRAPH_EDGE_HIT_SIZE,
                        "color": "rgba(255,255,255,0.001)",
                    },
                    hoverinfo="none",
                    showlegend=False,
                    name=edge.edge_key,
                )
            )

        figure.add_trace(
            go.Scatter(
                x=[item.x for item in self.relation_graph_nodes],
                y=[item.y for item in self.relation_graph_nodes],
                mode="markers+text",
                text=[item.name for item in self.relation_graph_nodes],
                textposition="top center",
                textfont={"color": "#D8F3FF", "size": 12},
                marker={
                    "size": [item.size for item in self.relation_graph_nodes],
                    "color": [item.color for item in self.relation_graph_nodes],
                    "line": {"width": 1.5, "color": "rgba(255,255,255,0.22)"},
                    "opacity": 0.95,
                },
                hoverinfo="none",
                showlegend=False,
                name="nodes",
            )
        )
        figure.update_layout(
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            margin={"l": 0, "r": 0, "t": 0, "b": 0},
            xaxis={"visible": False},
            yaxis={"visible": False},
            dragmode="pan",
            clickmode="event+select",
        )
        return figure

    @rx.var
    def relation_graph_plot_config(self) -> dict[str, Any]:
        return {
            "displayModeBar": False,
            "scrollZoom": True,
            "responsive": True,
        }

    @staticmethod
    def _message_to_dict(message: ChatMessage) -> dict[str, str]:
        data: dict[str, Any]
        if hasattr(message, "model_dump"):
            data = message.model_dump()
        else:
            data = message.dict()
        return {
            "role": str(data.get("role", "")),
            "content": str(data.get("content", "")),
        }

    def _trim_chat_messages(self):
        if len(self.chat_messages) <= MAX_CHAT_MESSAGES:
            return
        self.chat_messages = self.chat_messages[-MAX_CHAT_MESSAGES:]

    @staticmethod
    def _build_highlighted_excerpt_html(excerpt: str, claim: str) -> str:
        safe_excerpt = html.escape(str(excerpt or "").strip()).replace("\n", "<br>")
        safe_claim = html.escape(str(claim or "").strip()).replace("\n", "<br>")
        if not safe_excerpt:
            return ""
        if safe_claim and safe_claim in safe_excerpt:
            highlighted = safe_excerpt.replace(
                safe_claim,
                f"<span style=\"color:#67e8f9;font-weight:700;background:rgba(8,145,178,0.16);padding:0 0.14rem;border-radius:0.22rem;\">{safe_claim}</span>",
                1,
            )
            return highlighted
        if safe_claim:
            return (
                f"<div style=\"color:#67e8f9;font-weight:700;margin-bottom:0.45rem;\">{safe_claim}</div>"
                f"<div>{safe_excerpt}</div>"
            )
        return safe_excerpt

    @staticmethod
    def _normalize_confirmed_evidence(packet: dict[str, Any] | None) -> list[ConfirmedEvidence]:
        rows = packet.get("confirmed_evidence", []) if isinstance(packet, dict) and isinstance(packet.get("confirmed_evidence"), list) else []
        normalized: list[ConfirmedEvidence] = []
        for row in rows[:8]:
            if not isinstance(row, dict):
                continue
            chapter_index = int(row.get("chapter_index") or 0)
            if chapter_index <= 0:
                continue
            normalized.append(
                ConfirmedEvidence(
                    subquery_id=str(row.get("subquery_id", "") or ""),
                    label=str(row.get("label", "") or ""),
                    chapter_index=chapter_index,
                    source_name=str(row.get("source_name", "") or ""),
                    claim=str(row.get("claim", "") or "").strip(),
                    excerpt=str(row.get("excerpt", "") or "").strip(),
                    highlighted_html=NovelState._build_highlighted_excerpt_html(
                        str(row.get("excerpt", "") or "").strip(),
                        str(row.get("claim", "") or "").strip(),
                    ),
                    confidence=float(row.get("confidence", 0.0) or 0.0),
                )
            )
        return normalized

    @staticmethod
    def _normalize_character_archive_items(rows: list[dict[str, Any]] | None) -> list[CharacterArchiveCard]:
        normalized: list[CharacterArchiveCard] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            normalized.append(
                CharacterArchiveCard(
                    id=int(row.get("id") or 0),
                    name=str(row.get("name") or "").strip(),
                    alias_preview=[str(item or "").strip() for item in row.get("alias_preview") or [] if str(item or "").strip()],
                    alias_preview_text=" / ".join([str(item or "").strip() for item in row.get("alias_preview") or [] if str(item or "").strip()]),
                    record_count=int(row.get("record_count") or 0),
                    first_chapter_index=int(row.get("first_chapter_index") or 0),
                    last_chapter_index=int(row.get("last_chapter_index") or 0),
                )
            )
        return [item for item in normalized if item.id > 0 and item.name]

    @staticmethod
    def _normalize_character_profile(profile: dict[str, Any] | None) -> CharacterProfileView:
        source = profile if isinstance(profile, dict) else {}
        identity = source.get("identity", {}) if isinstance(source.get("identity"), dict) else {}
        volume_arc: list[CharacterVolumeArc] = []
        for item in source.get("volume_arc", []) if isinstance(source.get("volume_arc"), list) else []:
            if not isinstance(item, dict):
                continue
            volume_arc.append(
                CharacterVolumeArc(
                    volume_index=int(item.get("volume_index") or 0),
                    volume_title=str(item.get("volume_title") or "").strip(),
                    summary=str(item.get("summary") or "").strip(),
                    role_in_volume=[str(value or "").strip() for value in item.get("role_in_volume") or [] if str(value or "").strip()],
                    goals=[str(value or "").strip() for value in item.get("goals") or [] if str(value or "").strip()],
                    state_changes=[str(value or "").strip() for value in item.get("state_changes") or [] if str(value or "").strip()],
                    relationship_changes=[str(value or "").strip() for value in item.get("relationship_changes") or [] if str(value or "").strip()],
                )
            )
        emotional_relations: list[CharacterEmotionalRelationView] = []
        for item in source.get("emotional_relations", []) if isinstance(source.get("emotional_relations"), list) else []:
            if not isinstance(item, dict):
                continue
            timeline: list[CharacterEmotionalRelationTimeline] = []
            for entry in item.get("timeline", []) if isinstance(item.get("timeline"), list) else []:
                if not isinstance(entry, dict):
                    continue
                timeline.append(
                    CharacterEmotionalRelationTimeline(
                        chapter_start=int(entry.get("chapter_start") or 0),
                        chapter_end=int(entry.get("chapter_end") or 0),
                        summary=str(entry.get("summary") or "").strip(),
                    )
                )
            emotional_relations.append(
                CharacterEmotionalRelationView(
                    target_character_name=str(item.get("target_character") or item.get("target_character_name") or "").strip(),
                    relation_summary=str(item.get("relation_summary") or "").strip(),
                    primary_relation_type=str(item.get("primary_relation_type") or "").strip(),
                    secondary_emotional_tendencies=[str(value or "").strip() for value in item.get("secondary_emotional_tendencies") or [] if str(value or "").strip()],
                    intensity=str(item.get("intensity") or "").strip(),
                    current_status=str(item.get("current_status") or "").strip(),
                    timeline=[row for row in timeline if row.chapter_start > 0 and row.summary],
                )
            )
        style_samples: list[CharacterStyleSampleView] = []
        for item in source.get("style_samples", []) if isinstance(source.get("style_samples"), list) else []:
            if not isinstance(item, dict):
                continue
            style_samples.append(
                CharacterStyleSampleView(
                    scene=str(item.get("scene") or "").strip(),
                    quote=str(item.get("quote") or "").strip(),
                )
            )
        return CharacterProfileView(
            identity_summary=str(identity.get("summary") or "").strip(),
            aliases=_dedupe_non_empty_strs(identity.get("aliases") if isinstance(identity.get("aliases"), list) else []),
            appearance=_dedupe_non_empty_strs(source.get("appearance") if isinstance(source.get("appearance"), list) else []),
            narrative_role=_dedupe_non_empty_strs(source.get("narrative_role") if isinstance(source.get("narrative_role"), list) else []),
            personality_and_style=_dedupe_non_empty_strs(source.get("personality_and_style") if isinstance(source.get("personality_and_style"), list) else []),
            style_summary=str(source.get("style_summary") or "").strip(),
            speech_style=_dedupe_non_empty_strs(source.get("speech_style") if isinstance(source.get("speech_style"), list) else []),
            style_samples=[item for item in style_samples if item.scene and item.quote],
            goals_and_motivation=_dedupe_non_empty_strs(source.get("goals_and_motivation") if isinstance(source.get("goals_and_motivation"), list) else []),
            stance_and_alignment=_dedupe_non_empty_strs(source.get("stance_and_alignment") if isinstance(source.get("stance_and_alignment"), list) else []),
            abilities_and_resources=_dedupe_non_empty_strs(source.get("abilities_and_resources") if isinstance(source.get("abilities_and_resources"), list) else []),
            stable_profile=_dedupe_non_empty_strs(source.get("stable_profile") if isinstance(source.get("stable_profile"), list) else []),
            emotional_relations=[item for item in emotional_relations if item.target_character_name],
            volume_arc=volume_arc,
            current_state=_dedupe_non_empty_strs(source.get("current_state") if isinstance(source.get("current_state"), list) else []),
            turning_points=_dedupe_non_empty_strs(source.get("turning_points") if isinstance(source.get("turning_points"), list) else []),
            key_events=_dedupe_non_empty_strs(source.get("key_events") if isinstance(source.get("key_events"), list) else []),
        )

    @staticmethod
    def _normalize_character_relations(rows: list[dict[str, Any]] | None) -> list[CharacterRelationView]:
        normalized: list[CharacterRelationView] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            history: list[CharacterRelationHistory] = []
            for item in row.get("history_json") or []:
                if not isinstance(item, dict):
                    continue
                history.append(
                    CharacterRelationHistory(
                        chapter_start=int(item.get("chapter_start") or 0),
                        chapter_end=int(item.get("chapter_end") or 0),
                        relation_type=str(item.get("relation_type") or "").strip(),
                        structural_relation=[str(value or "").strip() for value in item.get("structural_relation") or [] if str(value or "").strip()],
                        action_relation=[str(value or "").strip() for value in item.get("action_relation") or [] if str(value or "").strip()],
                        emotional_relation=[str(value or "").strip() for value in item.get("emotional_relation") or [] if str(value or "").strip()],
                        polarity=str(item.get("polarity") or "").strip(),
                        strength=str(item.get("strength") or "").strip(),
                        directionality=str(item.get("directionality") or "").strip(),
                        stability=str(item.get("stability") or "").strip(),
                        current_status=str(item.get("current_status") or "").strip(),
                        drivers=[str(value or "").strip() for value in item.get("drivers") or [] if str(value or "").strip()],
                        summary=str(item.get("summary") or "").strip(),
                    )
                )
            relation_model = row.get("relation_model_json", {}) if isinstance(row.get("relation_model_json"), dict) else {}
            normalized.append(
                CharacterRelationView(
                    target_character_name=str(row.get("target_character_name") or "").strip(),
                    summary=str(row.get("summary") or "").strip(),
                    structural_relation=[str(value or "").strip() for value in relation_model.get("structural_relation") or [] if str(value or "").strip()],
                    action_relation=[str(value or "").strip() for value in relation_model.get("action_relation") or [] if str(value or "").strip()],
                    emotional_relation=[str(value or "").strip() for value in relation_model.get("emotional_relation") or [] if str(value or "").strip()],
                    directionality=str(relation_model.get("directionality") or "").strip(),
                    stability=str(relation_model.get("stability") or "").strip(),
                    current_status=str(relation_model.get("current_status") or "").strip(),
                    drivers=[str(value or "").strip() for value in relation_model.get("drivers") or [] if str(value or "").strip()],
                    history=[item for item in history if item.chapter_start > 0],
                )
            )
        return [item for item in normalized if item.target_character_name]

    def _apply_character_snapshot(self, snapshot: dict[str, Any] | None) -> None:
        payload = snapshot if isinstance(snapshot, dict) else {}
        character = payload.get("character", {}) if isinstance(payload.get("character"), dict) else {}
        if character:
            self.current_character_card = CharacterArchiveCard(
                id=int(character.get("id") or 0),
                name=str(character.get("name") or "").strip(),
                alias_preview=[str(item or "").strip() for item in character.get("alias_preview") or [] if str(item or "").strip()],
                alias_preview_text=" / ".join([str(item or "").strip() for item in character.get("alias_preview") or [] if str(item or "").strip()]),
                record_count=int(character.get("record_count") or 0),
                first_chapter_index=int(character.get("first_chapter_index") or 0),
                last_chapter_index=int(character.get("last_chapter_index") or 0),
            )
            self.current_character_id = int(character.get("id") or 0)
            self.current_character_name = str(character.get("name") or "").strip()
        self.current_character_profile = self._normalize_character_profile(payload.get("profile"))
        self.current_character_relations = self._normalize_character_relations(payload.get("relations"))

    @staticmethod
    def _normalize_relation_graph_nodes(rows: list[dict[str, Any]] | None) -> list[RelationGraphNodeView]:
        normalized: list[RelationGraphNodeView] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            normalized.append(
                RelationGraphNodeView(
                    graph_key=str(row.get("graph_key") or "").strip(),
                    book_id=int(row.get("book_id") or 0),
                    character_id=int(row.get("character_id") or 0),
                    name=str(row.get("name") or "").strip(),
                    aliases=[str(item or "").strip() for item in row.get("aliases") or [] if str(item or "").strip()],
                    profile_status=str(row.get("profile_status") or "stub"),
                    current_state_summary=str(row.get("current_state_summary") or "").strip(),
                    first_chapter_index=int(row.get("first_chapter_index") or 0),
                    last_chapter_index=int(row.get("last_chapter_index") or 0),
                    version_hash=str(row.get("version_hash") or "").strip(),
                    degree=int(row.get("degree") or 0),
                    x=float(row.get("x") or 0.0),
                    y=float(row.get("y") or 0.0),
                    size=float(row.get("size") or 18.0),
                    color=str(row.get("color") or "#38BDF8"),
                )
            )
        return [item for item in normalized if item.graph_key and item.name]

    @staticmethod
    def _normalize_relation_graph_edges(rows: list[dict[str, Any]] | None) -> list[RelationGraphEdgeView]:
        normalized: list[RelationGraphEdgeView] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            normalized.append(
                RelationGraphEdgeView(
                    edge_key=str(row.get("edge_key") or "").strip(),
                    book_id=int(row.get("book_id") or 0),
                    source_graph_key=str(row.get("source_graph_key") or "").strip(),
                    target_graph_key=str(row.get("target_graph_key") or "").strip(),
                    source_name=str(row.get("source_name") or "").strip(),
                    target_name=str(row.get("target_name") or "").strip(),
                    summary=str(row.get("summary") or "").strip(),
                    structural_relation=[str(item or "").strip() for item in row.get("structural_relation") or [] if str(item or "").strip()],
                    action_relation=[str(item or "").strip() for item in row.get("action_relation") or [] if str(item or "").strip()],
                    emotional_relation=[str(item or "").strip() for item in row.get("emotional_relation") or [] if str(item or "").strip()],
                    directionality=str(row.get("directionality") or "").strip(),
                    stability=str(row.get("stability") or "").strip(),
                    current_status=str(row.get("current_status") or "").strip(),
                    drivers=[str(item or "").strip() for item in row.get("drivers") or [] if str(item or "").strip()],
                    first_chapter_index=int(row.get("first_chapter_index") or 0),
                    last_chapter_index=int(row.get("last_chapter_index") or 0),
                    version_hash=str(row.get("version_hash") or "").strip(),
                    color=str(row.get("color") or "rgba(125,211,252,0.45)"),
                )
            )
        return [item for item in normalized if item.edge_key and item.source_graph_key and item.target_graph_key]

    @staticmethod
    def _normalize_relation_graph_node_detail(payload: dict[str, Any] | None) -> RelationGraphNodeDetailView:
        source = payload if isinstance(payload, dict) else {}
        return RelationGraphNodeDetailView(
            graph_key=str(source.get("graph_key") or "").strip(),
            book_id=int(source.get("book_id") or 0),
            character_id=int(source.get("character_id") or 0),
            name=str(source.get("name") or "").strip(),
            aliases=[str(item or "").strip() for item in source.get("aliases") or [] if str(item or "").strip()],
            profile_status=str(source.get("profile_status") or "stub"),
            current_state_summary=str(source.get("current_state_summary") or "").strip(),
            first_chapter_index=int(source.get("first_chapter_index") or 0),
            last_chapter_index=int(source.get("last_chapter_index") or 0),
            version_hash=str(source.get("version_hash") or "").strip(),
        )

    @staticmethod
    def _normalize_relation_graph_edge_detail(payload: dict[str, Any] | None) -> RelationGraphEdgeDetailView:
        source = payload if isinstance(payload, dict) else {}
        history: list[RelationGraphEventDetailView] = []
        for item in source.get("history") or []:
            if not isinstance(item, dict):
                continue
            history.append(
                RelationGraphEventDetailView(
                    event_key=str(item.get("event_key") or "").strip(),
                    chapter_start=int(item.get("chapter_start") or 0),
                    chapter_end=int(item.get("chapter_end") or 0),
                    relation_type=str(item.get("relation_type") or "").strip(),
                    polarity=str(item.get("polarity") or "").strip(),
                    strength=str(item.get("strength") or "").strip(),
                    directionality=str(item.get("directionality") or "").strip(),
                    stability=str(item.get("stability") or "").strip(),
                    current_status=str(item.get("current_status") or "").strip(),
                    summary=str(item.get("summary") or "").strip(),
                    evidence_chapters=[int(value) for value in item.get("evidence_chapters") or [] if int(value) > 0],
                )
            )
        return RelationGraphEdgeDetailView(
            edge_key=str(source.get("edge_key") or "").strip(),
            book_id=int(source.get("book_id") or 0),
            source_graph_key=str(source.get("source_graph_key") or "").strip(),
            target_graph_key=str(source.get("target_graph_key") or "").strip(),
            source_name=str(source.get("source_name") or "").strip(),
            target_name=str(source.get("target_name") or "").strip(),
            summary=str(source.get("summary") or "").strip(),
            structural_relation=[str(item or "").strip() for item in source.get("structural_relation") or [] if str(item or "").strip()],
            action_relation=[str(item or "").strip() for item in source.get("action_relation") or [] if str(item or "").strip()],
            emotional_relation=[str(item or "").strip() for item in source.get("emotional_relation") or [] if str(item or "").strip()],
            directionality=str(source.get("directionality") or "").strip(),
            stability=str(source.get("stability") or "").strip(),
            current_status=str(source.get("current_status") or "").strip(),
            drivers=[str(item or "").strip() for item in source.get("drivers") or [] if str(item or "").strip()],
            first_chapter_index=int(source.get("first_chapter_index") or 0),
            last_chapter_index=int(source.get("last_chapter_index") or 0),
            history=history,
        )

    def load_books(self):
        try:
            global _BOOK_STATUS_RECOVERY_DONE
            from rag.uploadBook import list_books, recover_interrupted_book_statuses

            if not _BOOK_STATUS_RECOVERY_DONE:
                recovered_count = recover_interrupted_book_statuses()
                if recovered_count > 0:
                    logger.warning(
                        "Recovered interrupted analysis statuses: processing -> pending (%d books).",
                        recovered_count,
                    )
                _BOOK_STATUS_RECOVERY_DONE = True

            status_map = {
                "pending": "待处理",
                "processing": "处理中",
                "completed": "已完成",
                "error": "分析失败",
            }
            rows = list_books()
            if is_agent_runtime_prewarm_enabled():
                try:
                    from agent.chat_agent import schedule_agent_runtime_prewarm

                    warm_book_ids = [
                        int(row.get("id") or 0)
                        for row in rows
                        if int(row.get("id") or 0) > 0 and str(row.get("status") or "") == "completed"
                    ]
                    schedule_agent_runtime_prewarm(warm_book_ids)
                except Exception:
                    logger.exception("Failed to schedule agent runtime prewarm.")
            self.uploaded_books = [
                Book(
                    id=int(row.get("id") or 0),
                    title=str(row.get("title", "")).strip(),
                    meta=(
                        f"{str(row.get('author', '未知')).strip() or '未知'} · "
                        f"{status_map.get(str(row.get('status', 'pending')), '待处理')} · "
                        f"{int(row.get('total_chapters') or 0)}章"
                    ),
                    cover=_resolve_book_cover(
                        str(row.get("cover_url") or ""),
                        title=str(row.get("title", "")).strip(),
                    ),
                    status=str(row.get("status") or "pending"),
                )
                for row in rows
                if str(row.get("title", "")).strip()
            ]
        except Exception:
            logger.exception("Failed to load books from MySQL.")

    async def open_character_archive(self):
        if self.current_book_id <= 0:
            self.character_feedback = "请先进入一本书的详情页。"
            self.character_feedback_is_error = True
            self.character_feedback_visible = True
            return

        self.character_archive_loading = True
        self._reset_character_feedback()
        self.character_archive_page = 1
        self.page_mode = "character_archive"
        yield

        try:
            from rag.character_profiles import list_book_character_cards

            rows = await asyncio.to_thread(list_book_character_cards, int(self.current_book_id), None)
            self.character_archive_items = self._normalize_character_archive_items(rows)
        except Exception:
            logger.exception("Failed to load character archive: book_id=%s", self.current_book_id)
            self.character_archive_items = []
            self.character_feedback = "角色档案加载失败，请检查数据库配置。"
            self.character_feedback_is_error = True
            self.character_feedback_visible = True
        finally:
            self.character_archive_loading = False
            yield

    async def open_relation_graph(self):
        if self.current_book_id <= 0:
            self.character_feedback = "请先进入一本书的详情页。"
            self.character_feedback_is_error = True
            self.character_feedback_visible = True
            return

        self.page_mode = "relation_graph"
        self.relation_graph_loading = True
        self.relation_graph_error = ""
        self.clear_relation_graph_selection()
        yield
        async for _ in self.load_relation_graph():
            yield

    async def load_relation_graph(self):
        self.relation_graph_loading = True
        self.relation_graph_error = ""
        self.relation_graph_nodes = []
        self.relation_graph_edges = []
        yield
        try:
            from relationGraph.query import load_book_relation_graph

            payload = await asyncio.to_thread(load_book_relation_graph, int(self.current_book_id))
            self.relation_graph_nodes = self._normalize_relation_graph_nodes(payload.get("nodes"))
            self.relation_graph_edges = self._normalize_relation_graph_edges(payload.get("edges"))
        except Exception as exc:
            logger.exception("Failed to load relation graph: book_id=%s", self.current_book_id)
            self.relation_graph_error = f"关系图加载失败：{exc}"
        finally:
            self.relation_graph_loading = False
            yield

    def clear_relation_graph_selection(self):
        self.relation_graph_selected_node_key = ""
        self.relation_graph_selected_edge_key = ""
        self.relation_graph_selected_kind = ""
        self.relation_graph_node_detail = RelationGraphNodeDetailView()
        self.relation_graph_edge_detail = RelationGraphEdgeDetailView()

    async def select_relation_graph_node(self, graph_key: str):
        self.relation_graph_selected_kind = "node"
        self.relation_graph_selected_node_key = str(graph_key or "").strip()
        self.relation_graph_selected_edge_key = ""
        self.relation_graph_edge_detail = RelationGraphEdgeDetailView()
        if not self.relation_graph_selected_node_key:
            self.relation_graph_node_detail = RelationGraphNodeDetailView()
            yield
            return
        self.relation_graph_node_detail = self._relation_graph_node_detail_from_view(
            self._find_relation_graph_node(self.relation_graph_selected_node_key)
        )
        yield
        try:
            from relationGraph.query import load_character_node_detail

            payload = await asyncio.to_thread(load_character_node_detail, int(self.current_book_id), self.relation_graph_selected_node_key)
            detail = self._normalize_relation_graph_node_detail(payload)
            if detail.graph_key:
                self.relation_graph_node_detail = detail
        except Exception:
            logger.exception("Failed to load relation graph node detail: book_id=%s graph_key=%s", self.current_book_id, graph_key)
        yield

    async def select_relation_graph_edge(self, edge_key: str):
        self.relation_graph_selected_kind = "edge"
        self.relation_graph_selected_edge_key = str(edge_key or "").strip()
        self.relation_graph_selected_node_key = ""
        self.relation_graph_node_detail = RelationGraphNodeDetailView()
        if not self.relation_graph_selected_edge_key:
            self.relation_graph_edge_detail = RelationGraphEdgeDetailView()
            yield
            return
        edge_view = self._find_relation_graph_edge(self.relation_graph_selected_edge_key)
        self.relation_graph_edge_detail = self._relation_graph_edge_detail_from_view(edge_view)
        yield
        try:
            from relationGraph.query import load_relation_edge_detail

            payload = await asyncio.to_thread(load_relation_edge_detail, int(self.current_book_id), self.relation_graph_selected_edge_key)
            detail = self._normalize_relation_graph_edge_detail(payload)
            if detail.edge_key:
                if detail.first_chapter_index <= 0:
                    detail.first_chapter_index = self.relation_graph_edge_detail.first_chapter_index
                if detail.last_chapter_index <= 0:
                    detail.last_chapter_index = self.relation_graph_edge_detail.last_chapter_index
                self.relation_graph_edge_detail = detail
        except Exception:
            logger.exception("Failed to load relation graph edge detail: book_id=%s edge_key=%s", self.current_book_id, edge_key)
        yield

    async def select_relation_graph_point(self, points: list[dict[str, Any]]):
        if not points:
            self.clear_relation_graph_selection()
            yield
            return
        point = points[0] if isinstance(points[0], dict) else {}
        curve_number = int(point.get("curveNumber") or 0)
        point_number = int(point.get("pointNumber") or 0)
        if curve_number < len(self.relation_graph_edges):
            edge = self.relation_graph_edges[curve_number]
            async for event in self.select_relation_graph_edge(edge.edge_key):
                yield event
            return
        if 0 <= point_number < len(self.relation_graph_nodes):
            node = self.relation_graph_nodes[point_number]
            async for event in self.select_relation_graph_node(node.graph_key):
                yield event

    async def open_character_detail(self, character_id: int):
        if self.current_book_id <= 0 or int(character_id or 0) <= 0:
            self.character_feedback = "角色详情加载失败：缺少书籍或角色信息。"
            self.character_feedback_is_error = True
            self.character_feedback_visible = True
            return

        self.character_detail_loading = True
        self._reset_character_feedback()
        self.page_mode = "character_detail"
        self.current_character_id = int(character_id)
        self.current_character_name = ""
        self.current_character_card = CharacterArchiveCard()
        self.current_character_profile = CharacterProfileView()
        self.current_character_relations = []
        self.chat_mode = "roleplay"
        self.chat_messages = self._roleplay_loading_chat_messages()
        self.chat_input = ""
        yield

        try:
            from rag.character_profiles import get_character_archive_snapshot

            snapshot = await asyncio.to_thread(get_character_archive_snapshot, int(self.current_book_id), int(character_id))
            self._apply_character_snapshot(snapshot)
            if not any(str(message.role or "") == "user" for message in self.chat_messages):
                self.chat_messages = self._roleplay_chat_messages()
            if bool(snapshot.get("has_cached_result")):
                self.character_feedback = "已加载缓存档案。"
                self.character_feedback_is_error = False
                self.character_feedback_visible = True
        except Exception:
            logger.exception(
                "Failed to load character detail: book_id=%s character_id=%s",
                self.current_book_id,
                character_id,
            )
            self.current_character_profile = CharacterProfileView()
            self.current_character_relations = []
            self.character_feedback = "角色详情加载失败，请稍后重试。"
            self.character_feedback_is_error = True
            self.character_feedback_visible = True
        finally:
            self.character_detail_loading = False
            yield

    @rx.event(background=True)
    async def generate_character_archive(self):
        async with self:
            if self.is_character_generating or self.current_book_id <= 0 or self.current_character_id <= 0:
                return
            book_id = int(self.current_book_id)
            character_id = int(self.current_character_id)
            character_name = self.current_character_name
            self.is_character_generating = True
            self.character_feedback = f"正在为《{self.current_novel}》中的《{character_name}》生成角色档案..."
            self.character_feedback_is_error = False
            self.character_feedback_visible = True

        try:
            from rag.character_profiles import generate_character_archive, get_character_archive_snapshot

            result = await asyncio.to_thread(generate_character_archive, book_id, character_id)
            snapshot = await asyncio.to_thread(get_character_archive_snapshot, book_id, character_id)
            async with self:
                self._apply_character_snapshot(snapshot)
                if bool(result.get("cached")):
                    self.character_feedback = f"《{character_name}》已存在可复用的角色档案，已直接载入缓存。"
                else:
                    self.character_feedback = f"《{character_name}》角色档案生成完成。"
                self.character_feedback_is_error = False
                self.character_feedback_visible = True
        except Exception as exc:
            logger.exception(
                "Failed to generate character archive: book_id=%s character_id=%s",
                book_id,
                character_id,
            )
            async with self:
                self.character_feedback = f"角色档案生成失败：{exc}"
                self.character_feedback_is_error = True
                self.character_feedback_visible = True
        finally:
            async with self:
                self.is_character_generating = False

    @rx.event(background=True)
    async def generate_character_roleplay_enhancement(self):
        async with self:
            if (
                self.is_character_generating
                or self.is_character_roleplay_generating
                or self.current_book_id <= 0
                or self.current_character_id <= 0
            ):
                return
            book_id = int(self.current_book_id)
            character_id = int(self.current_character_id)
            character_name = self.current_character_name
            self.is_character_roleplay_generating = True
            self.character_feedback = f"正在为《{self.current_novel}》中的《{character_name}》生成角色扮演加强信息..."
            self.character_feedback_is_error = False
            self.character_feedback_visible = True

        try:
            from rag.character_profiles import generate_character_roleplay_enhancement, get_character_archive_snapshot

            await asyncio.to_thread(generate_character_roleplay_enhancement, book_id, character_id)
            snapshot = await asyncio.to_thread(get_character_archive_snapshot, book_id, character_id)
            async with self:
                self._apply_character_snapshot(snapshot)
                self.character_feedback = f"《{character_name}》角色扮演加强信息生成完成。"
                self.character_feedback_is_error = False
                self.character_feedback_visible = True
        except Exception as exc:
            logger.exception(
                "Failed to generate character roleplay enhancement: book_id=%s character_id=%s",
                book_id,
                character_id,
            )
            async with self:
                self.character_feedback = f"角色扮演加强信息生成失败：{exc}"
                self.character_feedback_is_error = True
                self.character_feedback_visible = True
        finally:
            async with self:
                self.is_character_roleplay_generating = False

    async def send_message(self):
        text = self.chat_input.strip()
        if not text or self.is_generating:
            return

        request_started_at = time.perf_counter()
        request_id = f"chat-{int(time.time() * 1000)}"
        logger.info(
            "[ChatFlow][%s] received query. chars=%d page_mode=%s current_novel=%s",
            request_id,
            len(text),
            self.page_mode,
            self.current_novel.strip() or "N/A",
        )
        history = [self._message_to_dict(message) for message in self.chat_messages]
        self.chat_messages.append(ChatMessage(role="user", content=text))
        self.chat_input = ""
        self.is_generating = True
        logger.info("[ChatFlow][%s] frontend state updated. history_turns=%d", request_id, len(history))
        yield

        reply = "我暂时没有生成有效回复，请稍后再试。"
        dispatch_started_at = time.perf_counter()
        try:
            from agent.chat_agent import get_chat_agent
            from agent.cosplay_agent import get_cosplay_agent

            active_book = self._current_book()
            active_title = str(active_book.title or "").strip() if active_book else ""
            if self.page_mode == "character_detail" and self.current_character_id > 0:
                roleplay_context = self._build_roleplay_context_payload()
                agent = get_cosplay_agent()
                logger.info(
                    "[ChatFlow][%s] dispatching cosplay_agent.reply. novel_title=%s timeout_sec=%d character=%s",
                    request_id,
                    active_title or "GLOBAL",
                    1800,
                    self.current_character_name,
                )
                reply = await asyncio.wait_for(
                    asyncio.to_thread(
                        agent.reply,
                        text,
                        book_id=int(self.current_book_id or 0),
                        novel_title=active_title,
                        character_id=int(self.current_character_id or 0),
                        character_name=self.current_character_name,
                        roleplay_context=roleplay_context,
                        chat_history=history,
                    ),
                    timeout=1800,
                )
                self.confirmed_evidence = []
            else:
                agent = get_chat_agent()
                logger.info(
                    "[ChatFlow][%s] dispatching chat_agent.reply. novel_title=%s timeout_sec=%d chat_mode=%s",
                    request_id,
                    active_title or "GLOBAL",
                    1800,
                    self.chat_mode,
                )
                reply = await asyncio.wait_for(
                    asyncio.to_thread(
                        agent.reply,
                        text,
                        book_id=int(self.current_book_id or 0),
                        novel_title=active_title,
                        chat_history=history,
                    ),
                    timeout=1800,
                )
                packet = agent.get_last_search_packet(
                    active_title,
                    book_id=int(self.current_book_id or 0),
                )
                self.confirmed_evidence = self._normalize_confirmed_evidence(packet)
            logger.info(
                "[ChatFlow][%s] agent.reply finished. elapsed_sec=%.3f reply_chars=%d",
                request_id,
                time.perf_counter() - dispatch_started_at,
                len(str(reply)),
            )
        except asyncio.TimeoutError:
            logger.exception(
                "[ChatFlow][%s] chat_agent.reply timed out. elapsed_sec=%.3f",
                request_id,
                time.perf_counter() - dispatch_started_at,
            )
            reply = "检索超时，已中断本次请求。请缩小问题范围后重试。"
            self.confirmed_evidence = []
        except Exception as exc:
            logger.exception(
                "[ChatFlow][%s] failed to call chat agent. elapsed_sec=%.3f",
                request_id,
                time.perf_counter() - dispatch_started_at,
            )
            reply = (
                "调用 LLM 失败，请检查 `.env` 中的 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 配置。"
            )
            if str(exc):
                reply = f"{reply}\n错误信息：{exc}"
            self.confirmed_evidence = []
        finally:
            self.chat_messages.append(ChatMessage(role="assistant", content=reply))
            self._trim_chat_messages()
            self.is_generating = False
            answer_total_elapsed_sec = time.perf_counter() - request_started_at
            logger.info(
                "[ChatFlow][%s] response committed. answer_total_elapsed_sec=%.3f total_messages=%d",
                request_id,
                answer_total_elapsed_sec,
                len(self.chat_messages),
            )
            yield

    async def handle_upload(self, files: list[rx.UploadFile]):
        if self.is_uploading:
            return

        self.is_uploading = True
        self.upload_feedback = ""
        self.upload_feedback_is_error = False
        yield

        if not files:
            self.upload_feedback = "未检测到上传文件。"
            self.upload_feedback_is_error = True
            self.is_uploading = False
            logger.warning("Upload skipped: no files selected.")
            yield
            return

        latest_title = ""
        success_count = 0
        failure_count = 0
        last_error = ""

        for file in files:
            raw_name = str(getattr(file, "filename", "") or getattr(file, "name", "")).strip()
            file_name = Path(raw_name).name
            if not file_name:
                failure_count += 1
                last_error = "无法识别上传文件名。"
                logger.warning("Upload skipped: file name is empty.")
                continue
            if Path(file_name).suffix.lower() not in {".txt", ".epub"}:
                failure_count += 1
                last_error = f"{file_name} 不是 .txt 或 .epub 文件。"
                logger.warning("Upload skipped: unsupported file type %s", file_name)
                continue
            try:
                upload_data = await file.read()
                from rag.uploadBook import save_uploaded_book

                result = await asyncio.to_thread(save_uploaded_book, file_name, upload_data)
                latest_title = str(result.get("title", "")).strip() or Path(file_name).stem.strip()
                success_count += 1
                logger.warning("Upload success: %s", file_name)
            except Exception:
                failure_count += 1
                last_error = f"{file_name} 上传失败。"
                logger.exception("Failed to save uploaded book: %s", file_name)

        self.load_books()
        if success_count > 0:
            self.page_mode = "bookshelf"

        if success_count > 0 and failure_count == 0:
            self.upload_feedback = f"上传成功：{latest_title or f'{success_count} 本书'}"
            self.upload_feedback_is_error = False
        elif success_count > 0:
            self.upload_feedback = f"部分上传成功（成功 {success_count}，失败 {failure_count}）"
            self.upload_feedback_is_error = False
        else:
            self.upload_feedback = f"上传失败：{last_error or '请确认上传 .txt 或 .epub 文件'}"
            self.upload_feedback_is_error = True

        self.is_uploading = False
        yield rx.clear_selected_files("novel_upload")

        if success_count > 0:
            feedback_snapshot = self.upload_feedback
            await asyncio.sleep(5)
            if not self.upload_feedback_is_error and self.upload_feedback == feedback_snapshot:
                self.upload_feedback = ""
                self.upload_feedback_is_error = False
                yield

    async def start_chapter_analysis(self):
        if self.is_analyzing:
            return

        active_book = self._current_book()
        active_title = str(active_book.title or "").strip() if active_book else ""
        if not active_title:
            self.analysis_feedback = "请先在详情页选择一本书。"
            self.analysis_feedback_is_error = True
            self.analysis_feedback_visible = True
            return

        self.is_analyzing = True
        self.analysis_feedback_visible = False
        yield

        try:
            from rag.uploadBook import summarize_book_chapters

            result = await asyncio.to_thread(summarize_book_chapters, active_title)
            self.load_books()
            self.analysis_feedback = (
                f"《{active_title}》分析完成，已写入 {int(result.get('updated_chapters', 0))} 章概要。"
            )
            self.analysis_feedback_is_error = False
        except Exception:
            logger.exception("Failed to summarize chapters for book: %s", active_title)
            self.analysis_feedback = f"《{active_title}》分析失败，请检查 LLM 或数据库配置。"
            self.analysis_feedback_is_error = True
        finally:
            self.is_analyzing = False

        self.analysis_feedback_visible = True
        yield

        if not self.analysis_feedback_is_error:
            feedback_snapshot = self.analysis_feedback
            await asyncio.sleep(5)
            if self.analysis_feedback_visible and self.analysis_feedback == feedback_snapshot:
                self.analysis_feedback_visible = False
                self.analysis_feedback = ""
                self.analysis_feedback_is_error = False
                yield
