from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from agent.graph import apply_llm_network_settings

try:
    from agent.prompt import ROUTE_SKILL_PATH_MAPPING_PROMPT
except ModuleNotFoundError:  # pragma: no cover - direct script execution fallback
    import sys

    ROOT_FOR_IMPORT = Path(__file__).resolve().parents[3]
    if str(ROOT_FOR_IMPORT) not in sys.path:
        sys.path.append(str(ROOT_FOR_IMPORT))
    from agent.prompt import ROUTE_SKILL_PATH_MAPPING_PROMPT

logger = logging.getLogger(__name__)
ROOT_DIR = Path(__file__).resolve().parents[3]
ENV_OVERRIDE_VAR = "STORY2MEMORY_ENV_OVERRIDE"
ROUTE_SKILL_USE_API_ENV_VAR = "ROUTE_SKILL_USE_API"

QUERY_REWRITE_PROMPT = """
# Role
你是 Story2Memory 的检索 query 改写器。你的任务是把用户问题改写成适配不同检索工具的多路 query。

# Output
只输出 JSON，不要解释，不要 markdown。

# JSON Schema
{
  "normalized_question": "补全代词后的完整问题",
  "entity_query": "适合实体混合检索的关键词堆叠",
  "plot_query": "适合 plot/volume 混合检索的关键词堆叠",
  "chapter_query": "适合 chapter_summary 检索与核验的完整问题",
  "fulltext_query": "适合 chapter content 核验的明确取证指令",
  "answer_mode": "fact|reasoning|timeline|quote",
  "entity_hints": ["实体1", "实体2"]
}

# Rules
1. 必须保留原问题事实目标，不要改写成别的问题。
2. `entity_query` 和 `plot_query` 用短关键词，不写长句。
3. `chapter_query` 和 `fulltext_query` 用完整句。
4. 若存在代词、省略指代，优先根据上下文补全；无法补全就保持原样。
5. 不要编造书外知识。
"""

MULTI_QUERY_PLANNER_PROMPT = """
# Role
你是 Story2Memory 的统一检索规划器。你的任务是对一个用户问题进行多意图拆分、子问题标准化、检索入口规划和动态预算建议。

# Output
只输出 JSON，不要解释，不要 markdown。

# Allowed intent_type
- first_appearance
- identity_ability
- causal_motivation
- timeline_evolution
- quote_micro_detail
- existence_check
- ending_fate
- general_fact

# Allowed entry_tools
- retrieve_entity_edge_records
- hybrid_retrieve_characters
- hybrid_retrieve_origanizations
- hybrid_retrieve_special_existences
- hybrid_retrieve_world_rules
- hybrid_retrieve_chapter_summaries
- hybrid_retrieve_plots
- hybrid_retrieve_volumes

# Intent Hints
- first_appearance: 问第一次/首次出现在哪，优先找最早章节锚点。
- identity_ability: 问身份、能力、背景、定义，优先实体锚点再扩到 plot。
- causal_motivation: 问原因、动机、为什么，优先 plot 因果链。
- ending_fate: 问结局、最终状态、最后去向，优先找最后章节锚点。
- timeline_evolution: 问完整剧情/时间线/演变，优先找时间线关键节点和章节窗口。
- quote_micro_detail: 问原文/对白/逐字细节，尽快收敛到最小章节窗口。
- existence_check: 问是否存在，优先做实体或章节存在性核验。
- general_fact: 通用事实题，优先选择最容易拿到稳定章节坐标的路径。

# Tool Hints
- retrieve_entity_edge_records: 直接读实体最早/最后 records，适合 first_appearance 或 ending_fate。
- hybrid_retrieve_characters: 角色实体混合检索，返回实体 record 和 chapter 锚点。
- hybrid_retrieve_origanizations: 组织/势力实体混合检索，返回实体 record 和 chapter 锚点。
- hybrid_retrieve_special_existences: 特殊存在/物品混合检索，返回实体 record 和 chapter 锚点。
- hybrid_retrieve_world_rules: 世界规则/设定混合检索，返回规则 record 和 chapter 锚点。
- hybrid_retrieve_plots: plot 层混合检索，返回 plot 与章节窗口，适合时间线/因果链问题。
- hybrid_retrieve_volumes: volume 层混合检索，返回全局 plot 范围，适合宏观时间跨度问题。
- hybrid_retrieve_chapter_summaries: chapter_summary 层混合检索，只用于章节候选定位，不是最终原文证据。

# JSON Schema
{
  "normalized_user_query": "规范化后的原问题",
  "subqueries": [
    {
      "subquery_id": "sq1",
      "label": "身份",
      "user_goal": "子问题",
      "intent_type": "identity_ability",
      "priority": 1,
      "is_explicit": true,
      "entity_focus": ["实体"],
      "query_rewrites": {
        "normalized_question": "子问题",
        "entity_query": "关键词",
        "plot_query": "关键词",
        "chapter_query": "完整问题",
        "fulltext_query": "完整取证问题"
      },
      "entry_tools": ["hybrid_retrieve_characters", "hybrid_retrieve_chapter_summaries"]
    }
  ],
  "execution_policy": {
    "global_max_tool_calls": 8,
    "global_max_fulltext_windows": 2,
    "max_parallel_first_hop_calls": 4,
    "per_subquery_verify_min": 1,
    "recovery_enabled": true,
    "recovery_max_steps_global": 3,
    "recovery_max_steps_per_subquery": 2
  }
}

# Rules
1. 允许拆成 1 到 3 个子问题。
2. 显式多问必须拆开；广义“完整介绍/完整信息/全面分析”可做有限隐式拆分。
3. 不要凭空新增用户没问的子问题，除非是广义信息请求且拆分明显必要。
4. 每个子问题必须是完整、自然、无前缀污染的问题。
5. `entry_tools` 每个子问题最多 2 个，且必须来自允许列表。
6. `query_rewrites` 中：
   - `entity_query` / `plot_query` 用短关键词
   - `chapter_query` / `fulltext_query` 用完整句
7. 预算要根据问题复杂度给建议值，但不要失控。
"""


@dataclass(frozen=True)
class RouteStep:
    tool: str
    objective: str
    done_when: str


@dataclass
class RouteState:
    last_tool: str = ""
    results_count: int = -1
    confidence: float = 0.0
    new_coordinates_found: bool = False
    rerank_failed: bool = False
    embedding_failed: bool = False
    repeated_same_call: bool = False
    total_tool_calls: int = 0
    fulltext_tool_calls: int = 0

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "RouteState":
        raw = payload or {}
        return cls(
            last_tool=str(raw.get("last_tool", "") or ""),
            results_count=int(raw.get("results_count", -1) or -1),
            confidence=float(raw.get("confidence", 0.0) or 0.0),
            new_coordinates_found=bool(raw.get("new_coordinates_found", False)),
            rerank_failed=bool(raw.get("rerank_failed", False)),
            embedding_failed=bool(raw.get("embedding_failed", False)),
            repeated_same_call=bool(raw.get("repeated_same_call", False)),
            total_tool_calls=int(raw.get("total_tool_calls", 0) or 0),
            fulltext_tool_calls=int(raw.get("fulltext_tool_calls", 0) or 0),
        )


INTENT_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("existence_check", ("是否存在", "有没有", "有无", "是否出现")),
    ("first_appearance", ("第一次出场", "首次出场", "第一次出现", "首次出现", "初登场")),
    ("identity_ability", ("是谁", "身份", "什么能力", "能力", "背景")),
    ("causal_motivation", ("为什么", "动机", "原因", "为何", "怎么导致", "故意")),
    ("ending_fate", ("结局", "最后怎样", "最后怎么样", "下场", "最终如何", "后来怎么样")),
    ("quote_micro_detail", ("原话", "原文", "具体对白", "哪一句", "细节", "逐字")),
    ("timeline_evolution", ("完整剧情", "时间线", "发展", "演变", "全过程", "结局")),
]

ROUTE_CATALOG: dict[str, str] = {
    "existence_check": "确认人物/事件是否存在，优先做存在性核验。",
    "first_appearance": "定位首次出现章节与情节背景。",
    "identity_ability": "回答角色身份、能力、背景类问题。",
    "causal_motivation": "回答原因/动机/为什么类问题。",
    "ending_fate": "回答角色或事件的结局、最终状态、最后去向。",
    "quote_micro_detail": "需要原文、对白、逐字细节。",
    "timeline_evolution": "需要完整发展脉络或时间线。",
    "general_fact": "通用事实问题，默认路径。",
}

ALLOWED_INTENTS = set(ROUTE_CATALOG.keys())

COMMON_STOPWORDS = {
    "的",
    "了",
    "吗",
    "呢",
    "和",
    "与",
    "中",
    "里",
    "请问",
    "小说",
    "神秘复苏",
}

ORGANIZATION_MARKERS = ("组织", "势力", "阵营", "团队", "俱乐部", "国王组织")
WORLD_RULE_MARKERS = ("规则", "设定", "禁忌", "代价", "条件", "限制", "规律")
SPECIAL_EXISTENCE_MARKERS = ("厉鬼", "灵异", "鬼", "棺材钉", "八音盒", "特殊存在", "特殊物品")
MULTI_QUERY_CONNECTORS = ("以及", "并且", "同时", "还有", "而且", "并问")
IMPLICIT_EXPANSION_MARKERS = ("完整信息", "完整介绍", "全面分析", "资料", "信息")
PRONOUN_PREFIXES = ("她", "他", "它", "其", "这个人", "这个角色", "这个组织")


def _load_runtime_env() -> None:
    load_dotenv(dotenv_path=ROOT_DIR / ".env")
    override_path = os.getenv(ENV_OVERRIDE_VAR)
    if override_path:
        load_dotenv(dotenv_path=override_path, override=True)


def _route_skill_use_api() -> bool:
    _load_runtime_env()
    raw = str(os.getenv(ROUTE_SKILL_USE_API_ENV_VAR, "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _stringify_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    parts.append(text)
                continue
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
                    continue
            rendered = str(item).strip()
            if rendered:
                parts.append(rendered)
        return "\n".join(parts)
    return str(content).strip()


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    payload = str(raw or "").strip()
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    left = payload.find("{")
    right = payload.rfind("}")
    if left >= 0 and right > left:
        candidate = payload[left : right + 1]
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


@lru_cache(maxsize=1)
def _build_route_llm() -> ChatOpenAI | None:
    _load_runtime_env()
    api_key = (
        os.getenv("ROUTE_SKILL_API_KEY", "").strip()
        or os.getenv("LLM_API_KEY", "").strip()
    )
    if not api_key:
        logger.warning("[RouteSkill] missing API key, fallback to rule-based route planning.")
        return None

    model_name = (
        os.getenv("ROUTE_SKILL_MODEL", "").strip()
        or os.getenv("LLM_MODEL", "deepseek-v3.2").strip()
        or "deepseek-v3.2"
    )
    base_url = (
        os.getenv("ROUTE_SKILL_BASE_URL", "").strip()
        or os.getenv("LLM_BASE_URL", "").strip()
    )
    timeout = float(os.getenv("ROUTE_SKILL_TIMEOUT_SECONDS", "25").strip() or 25.0)

    kwargs: dict[str, Any] = {
        "model": model_name,
        "api_key": api_key,
        "temperature": 0,
        "timeout": timeout,
    }
    if base_url:
        kwargs["base_url"] = base_url
    apply_llm_network_settings(kwargs)
    return ChatOpenAI(**kwargs)


def _classify_intent_rule(user_query: str) -> str:
    text = (user_query or "").strip()
    if re.search(r"为什么.*叫|为何.*叫|为什么.*称为|为何.*称为|称号|名字来源|得名", text):
        return "identity_ability"
    for intent, keywords in INTENT_PATTERNS:
        if any(keyword in text for keyword in keywords):
            return intent
    return "general_fact"


def _classify_intent_via_api(user_query: str) -> str | None:
    if not _route_skill_use_api():
        return None
    llm = _build_route_llm()
    if llm is None:
        return None

    prompt = ROUTE_SKILL_PATH_MAPPING_PROMPT.format(
        user_query=user_query,
        route_catalog_json=json.dumps(ROUTE_CATALOG, ensure_ascii=False),
    )

    try:
        response = llm.invoke([SystemMessage(content=prompt)])
        payload = _extract_json_object(_stringify_content(getattr(response, "content", response)))
        if payload is None:
            return None
        intent_type = str(payload.get("intent_type", "") or "").strip()
        if intent_type in ALLOWED_INTENTS:
            return intent_type
    except Exception as exc:
        logger.warning("[RouteSkill] API path mapping failed, fallback to rules: %s", exc)
    return None


def classify_intent(user_query: str) -> tuple[str, str]:
    rule_intent = _classify_intent_rule(user_query)
    if rule_intent != "general_fact":
        return rule_intent, "rule_fallback"
    api_intent = _classify_intent_via_api(user_query)
    if api_intent:
        return api_intent, "api"
    return rule_intent, "rule_fallback"


def _extract_keywords(text: str) -> list[str]:
    parts = re.findall(r"[\u4e00-\u9fffA-Za-z0-9_]+", text or "")
    keywords: list[str] = []
    seen: set[str] = set()
    for token in parts:
        token = token.strip()
        if not token or token in COMMON_STOPWORDS:
            continue
        if token in seen:
            continue
        keywords.append(token)
        seen.add(token)
    return keywords


def _normalize_question(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return "请定位可回答该问题的章节级证据。"
    if not normalized.endswith(("？", "?", "。")):
        normalized += "？"
    return normalized


def _guess_anchor_entity(user_query: str) -> str:
    text = str(user_query or "").strip()
    matched = re.search(r"([\u4e00-\u9fffA-Za-z0-9_]{2,12})的", text)
    if matched:
        return matched.group(1).strip()
    matched = re.search(r"([\u4e00-\u9fffA-Za-z0-9_]{2,12})(?:是|是谁|有什么|如何|为什么)", text)
    if matched:
        return matched.group(1).strip()
    keywords = _extract_keywords(text)
    return keywords[0] if keywords else ""


def _restore_anchor_for_fragment(fragment: str, anchor_entity: str) -> str:
    text = str(fragment or "").strip()
    if not text:
        return ""
    if not anchor_entity:
        return text
    if text.startswith(PRONOUN_PREFIXES):
        for prefix in PRONOUN_PREFIXES:
            if text.startswith(prefix):
                return f"{anchor_entity}{text[len(prefix):]}"
    return text


def _derive_subquery_label(user_goal: str, intent_type: str) -> str:
    text = str(user_goal or "").strip()
    if "身份" in text or "是谁" in text or "背景" in text or "为什么叫" in text or "为何叫" in text or "称号" in text:
        return "身份"
    if "结局" in text or "最后" in text or "下场" in text:
        return "结局"
    if "第一次" in text or "首次" in text:
        return "首次出场"
    if "为什么" in text or "原因" in text or "动机" in text:
        return "原因"
    if "原话" in text or "原文" in text or "对白" in text:
        return "原文"
    if intent_type == "timeline_evolution":
        return "时间线"
    if intent_type == "existence_check":
        return "存在性"
    return "问题"


def _split_explicit_subqueries(user_query: str) -> list[str]:
    text = str(user_query or "").strip()
    if not text:
        return []
    anchor = _guess_anchor_entity(text)
    normalized = text
    for connector in MULTI_QUERY_CONNECTORS:
        normalized = normalized.replace(connector, "||")
    normalized = re.sub(r"[？?；;。]+", "||", normalized)
    parts = [_restore_anchor_for_fragment(part.strip(), anchor) for part in normalized.split("||")]
    cleaned = []
    seen: set[str] = set()
    for item in parts:
        normalized_item = item.strip("，, ")
        if len(normalized_item) < 2:
            continue
        if normalized_item in seen:
            continue
        cleaned.append(_normalize_question(normalized_item))
        seen.add(normalized_item)
    return cleaned


def _build_implicit_subqueries(user_query: str) -> list[str]:
    text = str(user_query or "").strip()
    if not text:
        return []
    if not any(marker in text for marker in IMPLICIT_EXPANSION_MARKERS):
        return []
    anchor = _guess_anchor_entity(text)
    if not anchor:
        return []
    return [
        _normalize_question(f"{anchor}的身份是什么"),
        _normalize_question(f"{anchor}的关键经历和结局如何"),
    ]


def decompose_user_query(user_query: str) -> list[dict[str, Any]]:
    explicit_parts = _split_explicit_subqueries(user_query)
    decomposition_source = "single"
    parts = explicit_parts
    if len(parts) >= 2:
        decomposition_source = "explicit"
    elif len(parts) <= 1:
        implicit_parts = _build_implicit_subqueries(user_query)
        if implicit_parts:
            parts = implicit_parts
            decomposition_source = "implicit"
        elif not parts:
            parts = [_normalize_question(user_query)]

    results: list[dict[str, Any]] = []
    for index, item in enumerate(parts[:3], start=1):
        results.append(
            {
                "subquery_id": f"sq{index}",
                "user_goal": item,
                "is_explicit": decomposition_source == "explicit",
                "decomposition_source": decomposition_source,
                "priority": index,
            }
        )
    return results


def _normalize_entry_tools(raw_tools: Any) -> list[str]:
    allowed_tools = {
        "retrieve_entity_edge_records",
        "hybrid_retrieve_characters",
        "hybrid_retrieve_origanizations",
        "hybrid_retrieve_special_existences",
        "hybrid_retrieve_world_rules",
        "hybrid_retrieve_chapter_summaries",
        "hybrid_retrieve_plots",
        "hybrid_retrieve_volumes",
    }
    if not isinstance(raw_tools, list):
        return []
    tools: list[str] = []
    for item in raw_tools:
        text = str(item or "").strip()
        if text in allowed_tools and text not in tools:
            tools.append(text)
    return tools[:2]


def _normalize_planner_subquery(item: dict[str, Any], index: int) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    user_goal = _normalize_question(item.get("user_goal", ""))
    if len(user_goal.strip("？?。")) < 2:
        return None
    intent_type = str(item.get("intent_type", "") or "").strip()
    if intent_type not in ALLOWED_INTENTS:
        return None
    rewrites = item.get("query_rewrites", {}) if isinstance(item.get("query_rewrites"), dict) else {}
    normalized_rewrites = {
        "normalized_question": _normalize_question(rewrites.get("normalized_question", user_goal)),
        "entity_query": str(rewrites.get("entity_query", "") or "").strip() or " ".join(_extract_keywords(user_goal)[:8]),
        "plot_query": str(rewrites.get("plot_query", "") or "").strip() or " ".join(_extract_keywords(user_goal)[:8]),
        "chapter_query": _normalize_question(rewrites.get("chapter_query", user_goal)),
        "fulltext_query": _normalize_question(rewrites.get("fulltext_query", user_goal)),
        "rewrite_source": "planner_api",
    }
    entity_domains = infer_entity_domains(user_goal, intent_type)
    entry_tools = _normalize_entry_tools(item.get("entry_tools")) or _entry_tools_for_plan(intent_type, entity_domains)
    if intent_type in {"first_appearance", "ending_fate"}:
        entry_tools = _entry_tools_for_plan(intent_type, entity_domains)
    priority = max(1, int(item.get("priority", index) or index))
    label = str(item.get("label", "") or "").strip() or _derive_subquery_label(user_goal, intent_type)
    is_explicit = bool(item.get("is_explicit", False))
    entity_focus = [str(value or "").strip() for value in item.get("entity_focus", []) if str(value or "").strip()] if isinstance(item.get("entity_focus"), list) else []
    plan = {
        "intent_type": intent_type,
        "intent_mapping_source": "planner_api",
        "entity_domains": entity_domains,
        "entity_grounding": {
            "enabled": bool(entity_domains),
            "tables": entity_domains,
            "apply_only_on_high_confidence": True,
        },
        "query_rewrites": normalized_rewrites,
        "entry_tools": entry_tools,
        "primary_route": [asdict(step) for step in route_template(intent_type, entity_domains)],
        "fallback_rules": fallback_rules(),
        "triggered_fallbacks": [],
        "controller": {
            "max_same_tool_same_args_retries": 1,
            "max_total_tool_calls": 6,
            "max_fulltext_windows": 2,
            "chapter_read_span_limit": 8,
            "stop_if_no_new_coordinates_twice": True,
            "require_fulltext_for_quote": intent_type == "quote_micro_detail",
        },
        "recovery": {
            "enabled": True,
            "entry_conditions": [
                "no_chapter_evidence",
                "low_confidence",
                "conflicting_evidence",
            ],
            "allowed_tools": [
                "hybrid_retrieve_characters",
                "hybrid_retrieve_origanizations",
                "hybrid_retrieve_special_existences",
                "hybrid_retrieve_world_rules",
                "hybrid_retrieve_chapter_summaries",
                "hybrid_retrieve_plots",
                "hybrid_retrieve_volumes",
                "retrieve_chapter_summaries",
                "retrieve_chapters",
                "retrieve_chapter_directory",
            ],
            "max_steps": 3,
            "max_extra_fulltext_reads": 1,
            "stop_rules": [
                "stable_chapter_or_fulltext_evidence",
                "repeated_same_tool_same_args",
                "no_new_coordinates_twice",
                "invalid_or_disallowed_action",
                "budget_exhausted",
            ],
        },
    }
    plan["guidance_text"] = render_route_guidance(plan)
    return {
        "subquery_id": str(item.get("subquery_id", "") or f"sq{index}"),
        "label": label,
        "user_goal": user_goal,
        "intent_type": intent_type,
        "priority": priority,
        "is_explicit": is_explicit,
        "decomposition_source": str(item.get("decomposition_source", "") or "planner"),
        "entity_focus": entity_focus,
        "query_rewrites": normalized_rewrites,
        "entry_tools": entry_tools,
        "plan": plan,
    }


def _plan_multi_search_route_fallback(user_query: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    decomposition = decompose_user_query(user_query)
    subqueries: list[dict[str, Any]] = []
    for item in decomposition[:3]:
        sub_plan = plan_search_route(str(item.get("user_goal", "") or ""), state=None)
        label = _derive_subquery_label(str(item.get("user_goal", "") or ""), str(sub_plan.get("intent_type", "general_fact") or "general_fact"))
        subqueries.append(
            {
                "subquery_id": item.get("subquery_id"),
                "label": label,
                "user_goal": item.get("user_goal"),
                "intent_type": sub_plan.get("intent_type", "general_fact"),
                "priority": item.get("priority", len(subqueries) + 1),
                "is_explicit": bool(item.get("is_explicit", False)),
                "decomposition_source": item.get("decomposition_source", "single"),
                "entity_focus": [],
                "query_rewrites": dict(sub_plan.get("query_rewrites", {}) or {}),
                "entry_tools": list(sub_plan.get("entry_tools", []) or [])[:2],
                "plan": sub_plan,
            }
        )

    aggregate_entity_domains: list[str] = []
    for subquery in subqueries:
        for domain in list(subquery.get("plan", {}).get("entity_domains", []) or []):
            if domain not in aggregate_entity_domains:
                aggregate_entity_domains.append(domain)

    return {
        "normalized_user_query": _normalize_question(user_query),
        "decomposition_source": subqueries[0].get("decomposition_source", "single") if subqueries else "single",
        "subqueries": subqueries,
        "entity_domains": aggregate_entity_domains,
        "entity_grounding": {
            "enabled": bool(aggregate_entity_domains),
            "tables": aggregate_entity_domains,
            "apply_only_on_high_confidence": True,
        },
        "execution_policy": {
            "global_max_tool_calls": 8,
            "global_max_fulltext_windows": 2,
            "max_parallel_first_hop_calls": 4,
            "per_subquery_verify_min": 1,
            "recovery_enabled": True,
            "recovery_max_steps_global": 3,
            "recovery_max_steps_per_subquery": 2,
        },
    }


def _plan_multi_search_route_via_api(user_query: str) -> dict[str, Any] | None:
    llm = _build_route_llm()
    if llm is None:
        return None
    try:
        response = llm.invoke(
            [
                SystemMessage(content=MULTI_QUERY_PLANNER_PROMPT),
                HumanMessage(content=f"user_query: {user_query}"),
            ]
        )
        payload = _extract_json_object(_stringify_content(getattr(response, "content", response)))
        if not isinstance(payload, dict):
            return None
        normalized_user_query = _normalize_question(payload.get("normalized_user_query", user_query))
        raw_subqueries = payload.get("subqueries", [])
        if not isinstance(raw_subqueries, list):
            return None
        subqueries: list[dict[str, Any]] = []
        for index, item in enumerate(raw_subqueries[:3], start=1):
            normalized = _normalize_planner_subquery(item, index)
            if normalized is not None:
                subqueries.append(normalized)
        if not subqueries:
            return None
        execution_policy = payload.get("execution_policy", {}) if isinstance(payload.get("execution_policy"), dict) else {}
        aggregate_entity_domains: list[str] = []
        for subquery in subqueries:
            for domain in list(subquery.get("plan", {}).get("entity_domains", []) or []):
                if domain not in aggregate_entity_domains:
                    aggregate_entity_domains.append(domain)
        return {
            "normalized_user_query": normalized_user_query,
            "decomposition_source": "planner",
            "subqueries": subqueries,
            "entity_domains": aggregate_entity_domains,
            "entity_grounding": {
                "enabled": bool(aggregate_entity_domains),
                "tables": aggregate_entity_domains,
                "apply_only_on_high_confidence": True,
            },
            "execution_policy": {
                "global_max_tool_calls": int(execution_policy.get("global_max_tool_calls", 8) or 8),
                "global_max_fulltext_windows": int(execution_policy.get("global_max_fulltext_windows", 2) or 2),
                "max_parallel_first_hop_calls": int(execution_policy.get("max_parallel_first_hop_calls", 4) or 4),
                "per_subquery_verify_min": int(execution_policy.get("per_subquery_verify_min", 1) or 1),
                "recovery_enabled": bool(execution_policy.get("recovery_enabled", True)),
                "recovery_max_steps_global": int(execution_policy.get("recovery_max_steps_global", 3) or 3),
                "recovery_max_steps_per_subquery": int(execution_policy.get("recovery_max_steps_per_subquery", 2) or 2),
            },
        }
    except Exception as exc:
        logger.warning("[RouteSkill] multi-query planner failed, fallback to rule planner: %s", exc)
        return None


def _fallback_query_rewrites(user_query: str, intent_type: str) -> dict[str, Any]:
    normalized_question = _normalize_question(user_query)
    keywords = _extract_keywords(user_query)
    keyword_stack = " ".join(keywords[:10]) if keywords else normalized_question.strip("？?")

    entity_query = keyword_stack
    plot_query = keyword_stack
    chapter_query = normalized_question
    fulltext_query = normalized_question
    answer_mode = "fact"

    if intent_type == "first_appearance":
        chapter_query = f"{normalized_question.rstrip('？?。')} 请确认首次出现章节和当时情境。"
        fulltext_query = f"{normalized_question.rstrip('？?。')} 请提取首次出场的直接原文依据。"
    elif intent_type == "causal_motivation":
        plot_query = f"{keyword_stack} 原因 动机 故意 导致"
        chapter_query = f"{normalized_question.rstrip('？?。')} 请确认直接原因、动机链条和关键章节。"
        fulltext_query = f"{normalized_question.rstrip('？?。')} 请提取能直接支持原因或动机判断的原文依据。"
        answer_mode = "reasoning"
    elif intent_type == "ending_fate":
        plot_query = f"{keyword_stack} 结局 最后 下场 最终"
        chapter_query = f"{normalized_question.rstrip('？?。')} 请确认最终状态、关键转折和章节依据。"
        fulltext_query = f"{normalized_question.rstrip('？?。')} 请提取能直接支持结局判断的原文依据。"
        answer_mode = "timeline"
    elif intent_type == "timeline_evolution":
        plot_query = f"{keyword_stack} 时间线 演变 过程 结局"
        chapter_query = f"{normalized_question.rstrip('？?。')} 请按时间顺序确认关键节点与对应章节。"
        fulltext_query = f"{normalized_question.rstrip('？?。')} 请提取关键转折节点的原文依据。"
        answer_mode = "timeline"
    elif intent_type == "quote_micro_detail":
        chapter_query = f"{normalized_question.rstrip('？?。')} 请先定位最可能出现原话的章节。"
        fulltext_query = f"{normalized_question.rstrip('？?。')} 请提取可直接引用的原文或对白。"
        answer_mode = "quote"
    elif intent_type == "identity_ability":
        plot_query = f"{keyword_stack} 身份 能力 背景"
        chapter_query = f"{normalized_question.rstrip('？?。')} 请确认身份定义、能力信息和章节依据。"
        fulltext_query = f"{normalized_question.rstrip('？?。')} 请提取可直接支持身份或能力判断的原文依据。"
        answer_mode = "reasoning"
    elif intent_type == "existence_check":
        chapter_query = f"{normalized_question.rstrip('？?。')} 请确认是否真实出现，并给出最早可定位章节。"
        fulltext_query = f"{normalized_question.rstrip('？?。')} 如存在，请提取最直接的原文依据。"

    return {
        "normalized_question": normalized_question,
        "entity_query": entity_query.strip(),
        "plot_query": plot_query.strip(),
        "chapter_query": chapter_query.strip(),
        "fulltext_query": fulltext_query.strip(),
        "hybrid_query": keyword_stack.strip(),
        "retrieve_intent": chapter_query.strip(),
        "answer_mode": answer_mode,
        "entity_hints": keywords[:4],
        "rewrite_source": "rule_fallback",
    }


def _rewrite_query_via_api(user_query: str, intent_type: str) -> dict[str, Any] | None:
    if not _route_skill_use_api():
        return None
    llm = _build_route_llm()
    if llm is None:
        return None

    prompt = (
        f"{QUERY_REWRITE_PROMPT}\n\n"
        f"intent_type: {intent_type}\n"
        f"user_query: {user_query}\n"
    )
    try:
        response = llm.invoke([SystemMessage(content=prompt)])
        payload = _extract_json_object(_stringify_content(getattr(response, "content", response)))
        if payload is None:
            return None
        normalized_question = _normalize_question(payload.get("normalized_question", user_query))
        entity_query = str(payload.get("entity_query", "") or "").strip()
        plot_query = str(payload.get("plot_query", "") or "").strip()
        chapter_query = str(payload.get("chapter_query", "") or "").strip()
        fulltext_query = str(payload.get("fulltext_query", "") or "").strip()
        answer_mode = str(payload.get("answer_mode", "") or "").strip() or "fact"
        entity_hints = [
            str(item or "").strip()
            for item in payload.get("entity_hints", [])
            if str(item or "").strip()
        ]
        if not entity_query or not plot_query or not chapter_query or not fulltext_query:
            return None
        return {
            "normalized_question": normalized_question,
            "entity_query": entity_query,
            "plot_query": plot_query,
            "chapter_query": chapter_query,
            "fulltext_query": fulltext_query,
            "hybrid_query": plot_query,
            "retrieve_intent": chapter_query,
            "answer_mode": answer_mode,
            "entity_hints": entity_hints[:4],
            "rewrite_source": "api",
        }
    except Exception as exc:
        logger.warning("[RouteSkill] query rewrite via API failed, fallback to rules: %s", exc)
        return None


def build_query_rewrites(user_query: str, intent_type: str) -> dict[str, Any]:
    api_payload = _rewrite_query_via_api(user_query, intent_type)
    if api_payload:
        return api_payload
    return _fallback_query_rewrites(user_query, intent_type)


def infer_entity_domains(user_query: str, intent_type: str) -> list[str]:
    text = str(user_query or "").strip()
    domains: list[str] = []
    if any(marker in text for marker in ORGANIZATION_MARKERS):
        domains.append("origanizations")
    if any(marker in text for marker in WORLD_RULE_MARKERS):
        domains.append("world_rules")
    if any(marker in text for marker in SPECIAL_EXISTENCE_MARKERS):
        domains.append("special_existences")
    if intent_type in {"identity_ability", "first_appearance", "existence_check"} and "world_rules" not in domains:
        domains.insert(0, "characters")
    if not domains and intent_type == "identity_ability":
        domains.append("characters")
    deduped: list[str] = []
    for item in domains:
        if item not in deduped:
            deduped.append(item)
    return deduped


def route_template(intent_type: str, entity_domains: list[str]) -> list[RouteStep]:
    domain_tools = {
        "characters": "hybrid_retrieve_characters",
        "origanizations": "hybrid_retrieve_origanizations",
        "special_existences": "hybrid_retrieve_special_existences",
        "world_rules": "hybrid_retrieve_world_rules",
    }
    entity_steps = [
        RouteStep(
            domain_tools[domain],
            f"用 {domain} 实体检索快速定位 chapter 锚点",
            "命中 chapter 锚点>=1 或明确无结果",
        )
        for domain in entity_domains
        if domain in domain_tools
    ]

    if intent_type == "first_appearance":
        return [
            RouteStep("retrieve_entity_edge_records", "直接读取实体最早 records，锁定首次出现章节锚点", "命中最早 records>=1"),
            RouteStep("retrieve_chapter_summaries", "核验首次出现上下文", "得到可回答的章节证据"),
            RouteStep("retrieve_chapters", "需要逐句证据时读取原文", "得到可直接引用原文"),
        ]
    if intent_type == "identity_ability":
        return entity_steps + [
            RouteStep("hybrid_retrieve_plots", "锁定身份/能力相关 plot 范围", "命中 plot>=1"),
            RouteStep("hybrid_retrieve_chapter_summaries", "补充 chapter 级锚点", "命中章节>=1"),
            RouteStep("retrieve_chapter_summaries", "章节级核验身份与能力", "有稳定章节证据"),
            RouteStep("retrieve_chapters", "需要细节时读取原文", "原文证据可直接引用"),
        ]
    if intent_type == "causal_motivation":
        return [
            RouteStep("hybrid_retrieve_plots", "定位因果链条可能发生的 plot", "命中 plot>=1"),
            RouteStep("hybrid_retrieve_chapter_summaries", "补充 chapter 级候选", "命中章节>=1"),
            RouteStep("retrieve_chapter_summaries", "核验原因/动机线索", "章节级因果线索成立"),
            RouteStep("retrieve_chapters", "最终读取关键原文", "关键因果原文可核验"),
        ]
    if intent_type == "ending_fate":
        return [
            RouteStep("retrieve_entity_edge_records", "直接读取实体最后 records，锁定结局相关章节锚点", "命中最后 records>=1"),
            RouteStep("retrieve_chapter_summaries", "核验最终状态和章节依据", "获得稳定结局证据"),
            RouteStep("retrieve_chapters", "必要时提取结局原文", "得到可引用原文"),
        ]
    if intent_type == "timeline_evolution":
        return entity_steps[:1] + [
            RouteStep("hybrid_retrieve_plots", "先锁定关键 plot 节点", "命中 plot>=1"),
            RouteStep("hybrid_retrieve_volumes", "补充全局范围和时间跨度", "命中 volume>=1 或确认无增益"),
            RouteStep("retrieve_chapter_summaries", "章节级核验关键时间线节点", "关键节点有章节证据"),
            RouteStep("retrieve_chapters", "关键转折需要原文时再读取", "关键节点原文可核验"),
        ]
    if intent_type == "quote_micro_detail":
        return [
            RouteStep("hybrid_retrieve_chapter_summaries", "先定位最可能出现原话的章节", "命中章节>=1"),
            RouteStep("retrieve_chapter_summaries", "收敛到最小章节窗口", "窗口范围稳定"),
            RouteStep("retrieve_chapters", "提取原文/对白", "得到逐字可核验证据"),
        ]
    if intent_type == "existence_check":
        return entity_steps + [
            RouteStep("hybrid_retrieve_chapter_summaries", "补充 chapter 层存在性核验", "命中章节或空结果"),
            RouteStep("hybrid_retrieve_plots", "需要时补充 plot 层交叉验证", "命中 plot 或空结果"),
            RouteStep("retrieve_chapter_summaries", "对命中结果做章节级确认", "存在/不存在结论可证据化"),
        ]
    return entity_steps + [
        RouteStep("hybrid_retrieve_chapter_summaries", "默认先做章节级混合初筛", "命中章节>=1"),
        RouteStep("hybrid_retrieve_plots", "补充 plot 定位", "命中 plot>=1"),
        RouteStep("retrieve_chapter_summaries", "章节级核验", "具备章节证据"),
        RouteStep("retrieve_chapters", "必要时提取原文", "细节证据可直接引用"),
    ]


def fallback_rules() -> list[dict[str, Any]]:
    return [
        {
            "when": "hybrid_no_hit_or_low_confidence",
            "condition": "results_count<=0 or confidence<0.45",
            "next_route": [
                "hybrid_retrieve_plots",
                "hybrid_retrieve_chapter_summaries",
                "hybrid_retrieve_volumes",
                "retrieve_chapter_summaries",
            ],
            "priority": 1,
        },
        {
            "when": "rerank_unavailable",
            "condition": "rerank_failed==true",
            "next_route": [
                "use_fused_order",
                "retrieve_chapter_summaries",
            ],
            "priority": 2,
        },
        {
            "when": "embedding_api_unavailable",
            "condition": "embedding_failed==true",
            "next_route": [
                "retrieve_chapter_summaries",
                "retrieve_chapters",
            ],
            "priority": 0,
        },
        {
            "when": "loop_risk",
            "condition": "repeated_same_call==true",
            "next_route": ["switch_level_up_or_down_immediately"],
            "priority": 0,
        },
        {
            "when": "no_new_coordinates",
            "condition": "new_coordinates_found==false",
            "next_route": ["switch_level_or_finalize"],
            "priority": 1,
        },
    ]


def _triggered_fallbacks(state: RouteState) -> list[str]:
    triggers: list[str] = []
    if state.results_count == 0 or state.confidence < 0.45:
        triggers.append("hybrid_no_hit_or_low_confidence")
    if state.rerank_failed:
        triggers.append("rerank_unavailable")
    if state.embedding_failed:
        triggers.append("embedding_api_unavailable")
    if state.repeated_same_call:
        triggers.append("loop_risk")
    if not state.new_coordinates_found:
        triggers.append("no_new_coordinates")
    return triggers


def _build_route_decision(
    primary_route: list[RouteStep],
    state: RouteState,
    triggered_names: set[str],
) -> dict[str, Any]:
    default_next_tool = primary_route[0].tool if primary_route else "hybrid_retrieve_chapter_summaries"
    decision = {
        "next_tool": default_next_tool,
        "decision_reason": "按 primary_route 首步执行。",
        "should_finalize": False,
    }

    if state.repeated_same_call:
        decision["next_tool"] = "switch_level"
        decision["decision_reason"] = "检测到同参重复调用，必须切换层级避免死循环。"
        return decision

    if state.total_tool_calls >= 6:
        decision["next_tool"] = "finalize_answer"
        decision["decision_reason"] = "达到总工具调用预算，停止继续扩检。"
        decision["should_finalize"] = True
        return decision

    if state.fulltext_tool_calls >= 2:
        decision["next_tool"] = "finalize_answer"
        decision["decision_reason"] = "达到原文读取预算，停止继续下潜。"
        decision["should_finalize"] = True
        return decision

    if "embedding_api_unavailable" in triggered_names:
        decision["next_tool"] = "retrieve_chapter_summaries"
        decision["decision_reason"] = "向量检索不可用，直接回到章节级验证。"
        return decision

    if "hybrid_no_hit_or_low_confidence" in triggered_names:
        for step in primary_route[1:]:
            if step.tool != state.last_tool:
                decision["next_tool"] = step.tool
                break
        decision["decision_reason"] = "当前召回不足，切换到下一层级补检。"
        return decision

    if "no_new_coordinates" in triggered_names and state.total_tool_calls >= 3:
        decision["next_tool"] = "finalize_answer"
        decision["decision_reason"] = "连续检索未带来新坐标，直接收束。"
        decision["should_finalize"] = True
        return decision

    return decision


def render_route_guidance(plan: dict[str, Any]) -> str:
    rewrites = plan.get("query_rewrites", {})
    route = plan.get("primary_route", [])
    route_decision = plan.get("route_decision", {})
    triggered = plan.get("triggered_fallbacks", [])
    controller = plan.get("controller", {})
    recovery = plan.get("recovery", {})

    lines: list[str] = [
        "[RetrievalRouteSkill Guidance]",
        f"- intent_type: {plan.get('intent_type', 'general_fact')}",
        f"- intent_mapping_source: {plan.get('intent_mapping_source', 'rule_fallback')}",
        f"- rewrite_source: {rewrites.get('rewrite_source', 'rule_fallback')}",
        f"- entity_query: {rewrites.get('entity_query', '')}",
        f"- plot_query: {rewrites.get('plot_query', '')}",
        f"- chapter_query: {rewrites.get('chapter_query', '')}",
        f"- fulltext_query: {rewrites.get('fulltext_query', '')}",
        f"- entity_domains: {','.join(plan.get('entity_domains', []))}",
        "- primary_route_priority:",
    ]
    for idx, step in enumerate(route, start=1):
        lines.append(
            f"  {idx}. {step.get('tool', '')} | 目标: {step.get('objective', '')} | 完成条件: {step.get('done_when', '')}"
        )
    if triggered:
        lines.append("- triggered_fallbacks:")
        for item in triggered:
            lines.append(f"  - {item.get('when', '')}: next_route={','.join(item.get('next_route', []))}")
    lines.extend(
        [
            "- route_decision:",
            f"  - next_tool: {route_decision.get('next_tool', '')}",
            f"  - reason: {route_decision.get('decision_reason', '')}",
            f"  - should_finalize: {route_decision.get('should_finalize', False)}",
            "- controller:",
            f"  - max_total_tool_calls: {controller.get('max_total_tool_calls', 6)}",
            f"  - max_same_tool_same_args_retries: {controller.get('max_same_tool_same_args_retries', 1)}",
            f"  - max_fulltext_windows: {controller.get('max_fulltext_windows', 2)}",
            f"  - chapter_read_span_limit: {controller.get('chapter_read_span_limit', 8)}",
            "- recovery:",
            f"  - enabled: {recovery.get('enabled', False)}",
            f"  - max_steps: {recovery.get('max_steps', 3)}",
            f"  - max_extra_fulltext_reads: {recovery.get('max_extra_fulltext_reads', 1)}",
            "硬性规则：只要检索不再产生新坐标或已达到预算，就立即收束，不再为 coverage 继续扩检。",
        ]
    )
    return "\n".join(lines)


def _entry_tools_for_plan(intent_type: str, entity_domains: list[str]) -> list[str]:
    domain_tool_map = {
        "characters": "hybrid_retrieve_characters",
        "origanizations": "hybrid_retrieve_origanizations",
        "special_existences": "hybrid_retrieve_special_existences",
        "world_rules": "hybrid_retrieve_world_rules",
    }
    entity_tools = [
        domain_tool_map[item]
        for item in entity_domains
        if item in domain_tool_map
    ]
    if intent_type == "identity_ability":
        return (entity_tools[:1] or ["hybrid_retrieve_characters"]) + ["hybrid_retrieve_chapter_summaries"]
    if intent_type == "first_appearance":
        return ["retrieve_entity_edge_records"]
    if intent_type == "existence_check":
        return (entity_tools[:1] or ["hybrid_retrieve_characters"]) + ["hybrid_retrieve_chapter_summaries"]
    if intent_type == "causal_motivation":
        return ["hybrid_retrieve_plots", "hybrid_retrieve_chapter_summaries"]
    if intent_type == "ending_fate":
        return ["retrieve_entity_edge_records"]
    if intent_type == "timeline_evolution":
        if entity_tools:
            return entity_tools[:1] + ["hybrid_retrieve_plots"]
        return ["hybrid_retrieve_plots", "hybrid_retrieve_volumes"]
    if intent_type == "quote_micro_detail":
        return ["hybrid_retrieve_chapter_summaries"]
    if entity_tools:
        return entity_tools[:1] + ["hybrid_retrieve_chapter_summaries"]
    return ["hybrid_retrieve_chapter_summaries", "hybrid_retrieve_plots"]


def plan_search_route(user_query: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    intent_type, intent_mapping_source = classify_intent(user_query)
    rewrites = build_query_rewrites(user_query, intent_type)
    entity_domains = infer_entity_domains(user_query, intent_type)

    steps = route_template(intent_type, entity_domains)
    fallback = fallback_rules()
    route_state = RouteState.from_dict(state)
    is_initial_route = state is None
    triggered_names = set() if is_initial_route else set(_triggered_fallbacks(route_state))
    triggered_rules = sorted(
        [rule for rule in fallback if rule["when"] in triggered_names],
        key=lambda item: int(item.get("priority", 99)),
    )
    decision = _build_route_decision(steps, route_state, triggered_names)

    entry_tools = _entry_tools_for_plan(intent_type, entity_domains)

    plan = {
        "intent_type": intent_type,
        "intent_mapping_source": intent_mapping_source,
        "entity_domains": entity_domains,
        "entity_grounding": {
            "enabled": bool(entity_domains),
            "tables": entity_domains,
            "apply_only_on_high_confidence": True,
        },
        "query_rewrites": rewrites,
        "entry_tools": entry_tools[:3],
        "primary_route": [asdict(step) for step in steps],
        "fallback_rules": fallback,
        "triggered_fallbacks": triggered_rules,
        "controller": {
            "max_same_tool_same_args_retries": 1,
            "max_total_tool_calls": 6,
            "max_fulltext_windows": 2,
            "chapter_read_span_limit": 8,
            "stop_if_no_new_coordinates_twice": True,
            "require_fulltext_for_quote": intent_type == "quote_micro_detail",
        },
        "recovery": {
            "enabled": True,
            "entry_conditions": [
                "no_chapter_evidence",
                "low_confidence",
                "conflicting_evidence",
            ],
            "allowed_tools": [
                "hybrid_retrieve_characters",
                "hybrid_retrieve_origanizations",
                "hybrid_retrieve_special_existences",
                "hybrid_retrieve_world_rules",
                "hybrid_retrieve_chapter_summaries",
                "hybrid_retrieve_plots",
                "hybrid_retrieve_volumes",
                "retrieve_chapter_summaries",
                "retrieve_chapters",
                "retrieve_chapter_directory",
            ],
            "max_steps": 3,
            "max_extra_fulltext_reads": 1,
            "stop_rules": [
                "stable_chapter_or_fulltext_evidence",
                "repeated_same_tool_same_args",
                "no_new_coordinates_twice",
                "invalid_or_disallowed_action",
                "budget_exhausted",
            ],
        },
        "route_decision": decision,
        "route_decision_source": "rule",
    }
    plan["guidance_text"] = render_route_guidance(plan)
    return plan


def plan_multi_search_route(user_query: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    planned = _plan_multi_search_route_via_api(user_query)
    if planned is not None:
        return planned
    return _plan_multi_search_route_fallback(user_query, state=state)
