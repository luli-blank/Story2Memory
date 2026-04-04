---
name: retrieval-route-skill
description: Route planning skill for Story2Memory search agent. Classifies query intent, prescribes tool priority, defines fallback transitions, and enforces early-stop rules to avoid broad unnecessary scans.
---

# Retrieval Route Skill

## Goal
Given `user_query` + runtime `state`, produce a planner-style retrieval policy for the Story2Memory search agent.

Default flow:
1. LLM planner decomposes the user query into 1-3 subqueries.
2. Each subquery gets:
   - normalized `user_goal`
   - `intent_type`
   - query rewrites
   - entry tools
3. Planner also suggests dynamic execution budgets.
4. Runtime validates/clamps planner output and executes a shared-grounding, multi-branch search.
5. If planner output is invalid or times out, fallback to the rule-based single/multi-query route logic.

## Input
- `user_query` (required)
- `state` (optional, recovery/runtime feedback)

## Output
Primary runtime entrypoint:
- `normalized_user_query`
- `subqueries`
  - `subquery_id`
  - `label`
  - `user_goal`
  - `intent_type`
  - `priority`
  - `is_explicit`
  - `entity_focus`
  - `query_rewrites`
  - `entry_tools`
  - `plan`
- `entity_grounding`
- `execution_policy`

Fallback/runtime-compatible single-query output may still include:
- `intent_type`
- `query_rewrites`
- `primary_route`
- `fallback_rules`
- `recovery`

## Runtime Contract
- Every search request should attempt the LLM planner first.
- Planner should decompose explicit multi-question prompts, and may do limited implicit decomposition for broad “完整信息/全面分析” requests.
- Runtime shares one grounding result and one evidence pool across all subqueries.
- Execution is phased:
  - parallel first hop
  - per-subquery verify
  - unresolved-only fulltext
  - unresolved-only recovery
- For `first_appearance`, prefer `retrieve_entity_edge_records(edge="first")` as the first hop.
- For `ending_fate`, prefer `retrieve_entity_edge_records(edge="last")` as the first hop.
- If query clearly names a role/organization/rule/special existence, prefer high-confidence alias grounding before broad entity retrieval.
- Avoid repeated same tool+same args loops.
- If planner output is invalid or times out, runtime must fall back safely to rule-based planning.
