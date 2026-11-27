# ai-memory — Agent Map

Vendor-neutral, provenance-aware personal AI memory service (PostgreSQL canonical store, Qdrant semantic index, FastAPI REST + MCP interfaces, OAuth gateway).

## Validation Gate
- Run the application and gateway test suites:
  ```sh
  make check
  ```
- Before pushing to public remotes, run the publication guard:
  ```sh
  make check-push
  ```

## Documentation Map
- [README.md](README.md) — Architecture overview, development setup, and client behavior.
- [OPERATIONS.md](OPERATIONS.md) — Deployment, backup, and operational runbook.
- [PRIVATE-DEPLOYMENT.md](PRIVATE-DEPLOYMENT.md) — Keeping private overrides outside tracked public source.
- [PUBLICATION.md](PUBLICATION.md) & [PUSH-PROTECTION.md](PUSH-PROTECTION.md) — Public release hygiene and gitleaks rules.

## Repository Layout
- `src/ai_memory/` — Core Python package (`cli.py`, FastAPI app, SQLAlchemy models, Qdrant indexing, MCP server, importers).
- `gateway/` — OAuth2/MCP gateway and its `requirements.txt`.
- `tests/` — Pytest unit/integration suite (`test_live_stack.py` is opt-in against a live deployment).
- `deploy/` & `compose.yaml` — Example deployment templates (never commit real hostnames, tokens, or private paths).

## Guardrails
- PostgreSQL is canonical; Qdrant is derived and rebuildable.
- This repository is published publicly: never commit private hostnames, personal paths, secrets, or deployment artifacts.
- Keep `pyproject.toml` and `gateway/requirements.txt` compatible with `make check` and CI (`.github/workflows/ci.yml`).
