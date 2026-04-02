from __future__ import annotations

import json

try:
    from agent.skills.retrieval_route_skill.route_skill import plan_search_route
except ModuleNotFoundError:  # pragma: no cover - local direct execution fallback
    from route_skill import plan_search_route


def run_demo() -> None:
    cases = [
        {
            "query": "苗小善的表哥是谁 第一次出场是什么时候",
            "state": {"results_count": 0, "confidence": 0.2, "embedding_failed": True, "total_tool_calls": 1},
        },
        {
            "query": "庄园主是谁 有什么能力",
            "state": {"results_count": 6, "confidence": 0.61, "rerank_failed": True, "last_tool": "hybrid_retrieve_plots"},
        },
        {
            "query": "杨间和张洞关系发展时间线",
            "state": {"results_count": 2, "confidence": 0.5, "has_chapter_evidence": False, "repeated_same_call": True},
        },
    ]

    for index, case in enumerate(cases, start=1):
        plan = plan_search_route(case["query"], case.get("state"))
        print(f"\n=== Demo Case {index} ===")
        print(json.dumps(plan, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run_demo()
