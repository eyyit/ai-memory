# AI Memory

Self-hosted memory with PostgreSQL canonical records, provenance and durable
jobs; Qdrant semantic indexing; CPU inference; REST and MCP interfaces.

The source includes importers for assistant JSON exports, Gemini activity
HTML, supported export archives, Git metadata, and shell history. Imports
are incremental and apply pattern-based secret redaction. Review imported
facts and sensitive material; redaction is not a guarantee.

## Client behavior

Connecting MCP does not automatically save conversations. Client instructions
can encourage relevant retrieval and confirmed durable-fact saves. Each
client's tool availability and approval policy still apply. The service does
not passively capture transcripts or export inaccessible native memories.

Usage counters retain 30 days of hourly aggregates, including operation,
outcome, result counts, and average/max duration. Query text, memory contents,
tokens, and raw client identities are not stored by those counters. Monitoring
traffic is separate. Counters are best effort, not lossless audit records.

## Development

Use Python 3.11 or newer and install development dependencies:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest
```

OAuth gateway tests also require gateway/requirements.txt. CPU model tests
and live-service tests have separate dependencies and runtime requirements.

## Deployment examples

The Compose files are examples requiring adaptation, not an installer.
Start with OPERATIONS.md. Replace example.com and documentation-network
addresses, configure private credentials, prepare model files and host paths,
and build images before starting. Never use example keys in a live instance.
The templates expose no public database/vector ports.

PostgreSQL is canonical; Qdrant is derived. Protect backup archives because
they can contain credentials and source evidence. Keep real data, secrets,
exports, models, environment files, and backups outside this repository.

This is a fresh sanitized source snapshot. Private deployment history and
operator review bundles are intentionally absent. No license grant is added
by this export; choose a license before inviting external reuse.
