# Agent Skills (Isolated)

This folder contains isolated skill prototypes for retrieval strategy planning.

Scope of this folder:
- Define query-to-route planning logic for search workflows.
- Define fallback strategy when a route does not produce usable evidence.
- Keep implementation fully decoupled from current runtime code.

Out of scope:
- No registration to current chat agent.
- No direct calls into existing tool code.
- No modifications outside `agent/skills`.

