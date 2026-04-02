# Story2Memory Open-Source Dockerization Design

## Context

Story2Memory is being prepared for open-source release on GitHub. The current repository already contains partial containerization for infrastructure services, but the application runtime is not yet packaged into a single, newcomer-friendly workflow.

Current observations from the repository:

- `docker-compose.yml` includes MySQL, Redis, Neo4j, `web-search`, and `rerank-local`, but does not include the main Reflex application container.
- The codebase already depends on Qdrant-related functionality, but Qdrant is not currently part of the compose stack.
- Runtime startup is inconsistent:
  - `reflex run` is the active application path.
  - `Makefile` still references `uvicorn app.main:create_app`, but `app/main.py` does not exist.
- `.env` currently contains real local configuration and sensitive keys. This must not become part of the public repository history.

## Product Goal

The open-source repository should support this primary user path:

1. Clone the repository.
2. Copy `.env.example` to `.env`.
3. Fill in external API credentials if needed.
4. Run one Docker command.
5. Open the app and use the core workflow without manual environment assembly.

The guiding principle is not "put everything into Docker at any cost". The guiding principle is:

- one obvious startup path
- reproducible runtime
- clear service boundaries
- low-friction onboarding for new users

## Decisions

### 1. Containerization model

Adopt a single `app` container for the Reflex application, plus dedicated dependency containers for stateful and auxiliary services.

Recommended default stack:

- `app`
- `mysql`
- `qdrant`
- `neo4j`
- `redis`
- `rerank-local`
- `web-search`

Reasoning:

- The current application structure is closer to a Reflex-driven integrated app than to a cleanly separated frontend SPA plus backend API.
- Forcing an early frontend/backend split would create packaging complexity without clear user benefit.
- A single application container keeps the first open-source release simpler while still allowing later decomposition if needed.

### 2. External LLM boundary

LLM providers are not containerized. They remain external dependencies configured via environment variables.

That means:

- `.env.example` includes all required `LLM_*`, embedding, and rerank configuration keys.
- Example values are placeholders only.
- Real API keys are always user-supplied.

This keeps the open-source stack realistic and avoids pretending that remote commercial APIs are part of the local deployment.

### 3. One canonical startup path

The repository should have one canonical startup flow for open-source users:

```bash
cp .env.example .env
docker compose up --build
```

This should become the primary README path.

Alternative development workflows can still exist later, but they should be secondary and documented as contributor workflows rather than newcomer defaults.

## Target Runtime Topology

### Application container

`app` is responsible for:

- running the Reflex application
- loading the unified `.env`
- connecting to service containers via internal Docker DNS names
- serving the web UI and app backend from one containerized runtime

The application must stop depending on host-only loopback addresses such as `127.0.0.1:<port>` for its internal service graph. Inside Docker, service-to-service communication should use names like:

- `mysql`
- `qdrant`
- `neo4j`
- `redis`
- `rerank-local`
- `web-search`

### Dependency containers

- `mysql`: primary relational store
- `qdrant`: vector storage used by the retrieval pipeline
- `neo4j`: graph storage for relation graph sync
- `redis`: optional buffer/cache support, but included in default stack for simplicity
- `rerank-local`: local reranker service
- `web-search`: auxiliary search service already present in the compose file

## Configuration Design

### `.env.example`

Create a committed `.env.example` that contains every configuration item needed for the default containerized path.

Recommended sections:

1. App
2. Docker-exposed ports
3. MySQL
4. Qdrant
5. Neo4j
6. Redis
7. LLM
8. Embedding
9. Rerank
10. Search / optional integrations
11. Pipeline behavior flags

Example conventions:

- placeholder values only
- inline comments that explain what is required
- group keys by function, not by implementation file
- keep internal container hostnames aligned with compose service names

### `.env`

`.env` remains local-only and gitignored.

The repository must not retain or reintroduce real credentials in committed files. Before publishing, any exposed secrets currently present in local `.env` should be rotated.

## Compose Behavior

### Default behavior

The default `docker compose up --build` should bring up all first-party services needed for the supported demo path.

### Health and ordering

Use healthchecks and `depends_on` conditions so `app` does not start before foundational services are actually reachable.

At minimum:

- MySQL healthcheck
- Qdrant healthcheck
- Neo4j healthcheck
- rerank-local healthcheck
- optional app-side wait/retry logic for service readiness

### Persistence

Use named volumes for:

- MySQL data
- Qdrant data
- Neo4j data
- Redis data if persistence is desired
- rerank model cache

This keeps first-run downloads and application state stable across restarts.

## Initialization Strategy

The default startup path must define what "ready to use" means.

Recommended baseline:

- schema bootstrap for MySQL
- Neo4j initialization script
- app startup only after essential services pass health checks

Open question for later implementation detail:

- whether initialization is performed by:
  - a dedicated `init` container/job
  - `app` startup hooks
  - explicit `make init` / `docker compose run` tasks

For first open-source usability, automatic or near-automatic initialization is preferred over manual multi-step setup.

## Required Cleanup Before Implementation

The runtime surface should be simplified before or during Docker work:

1. Remove or replace stale startup paths in `Makefile`.
2. Align all runtime environment loading around the Docker-first path.
3. Add missing Qdrant service configuration to compose and environment examples.
4. Normalize hostnames and DSNs to container-network addresses.
5. Make README reflect the new canonical startup path.

## Non-Goals For This Phase

These should not block the first Docker-based open-source release:

- perfect separation of frontend and backend into independent deployable artifacts
- production-grade orchestration beyond Docker Compose
- secret management beyond local `.env`
- support for every optional remote/local provider combination on day one

The first release should optimize for clarity and reproducibility, not maximum flexibility.

## Implementation Phasing

### Phase 1: Runtime normalization

- decide the single application entrypoint
- remove dead startup paths
- define the full environment contract

### Phase 2: Container packaging

- add Dockerfile for `app`
- extend compose with missing services and health checks
- wire internal service hostnames

### Phase 3: Onboarding path

- add `.env.example`
- update README
- validate first-run experience from a clean machine state

### Phase 4: Optional contributor ergonomics

- add secondary contributor workflows if still needed
- optionally add compose profiles later

## Risks

### 1. Over-containerizing an unstable runtime surface

If the current startup model remains ambiguous, Docker will only hide the ambiguity rather than remove it.

Mitigation:

- define the canonical entrypoint first

### 2. Secret leakage during open-source preparation

The repository currently relies on a populated `.env`. Publishing without credential hygiene would be a critical mistake.

Mitigation:

- replace committed examples with placeholders
- ensure `.env` is ignored
- rotate any real keys already used in development

### 3. First-run weight

Including all services by default increases startup time and resource usage, especially with local rerank model downloads.

Mitigation:

- accept this tradeoff for the first "one command" experience
- optimize later with profiles or optional modes only after the default path is stable

## Recommendation

Proceed with a Docker-first open-source release based on:

- one `app` container
- one full default compose stack
- one complete `.env.example`
- one canonical onboarding path

This is the most pragmatic way to make Story2Memory usable by new users without prematurely forcing a deeper service decomposition.
