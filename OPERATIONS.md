# Deployment example notes

Adapt the example to your environment before execution. No live deployment
or OAuth onboarding is claimed for these public templates.

1. Check deploy/compose.yaml and deploy/compose.defaults.yaml. The optional
   gateway is in deploy/oauth/compose.yaml. Choose your own HTTPS hostname.
2. Replace documentation-network addresses and /srv, /var/lib/containers,
   /var/log/containers and /etc/ai-memory/secrets example paths as needed.
   Review hostname allowlists and the gateway's public-base URL validation.
3. Generate credentials locally. Keep private values outside Git. The
   credential preparation examples must be reviewed before running as root.
4. Prepare the pinned model snapshots using scripts/maintenance.py. Inference
   is CPU-based and runtime model downloads are disabled in the example.
5. Build the application, inference and wrapper images and configure the
   matching PostgreSQL role. Apply deploy/migrations/usage-counters.sql if
   enabling usage statistics. Its role name must match your application role.

The root Compose file includes the core deployment. Compose include support
is required. Shared defaults are provided here, not in another private repo.
The HTTPS gateway needs an appropriate reverse proxy, OAuth registration,
and an egress path. A browser-login proxy alone is not an MCP OAuth provider.
No ports are published by the templates; choose an explicit access design.

Host-specific Borg, systemd, and monitoring examples are optional. They are
not installed automatically. Configure log rotation on your host. Recovery
scripts require their documented tools and filesystem layout; review them
before an actual restore. Do not restore into production merely to test.

Verify authentication, read/write scopes, source provenance, duplicate import
behavior, indexing, searches, usage-counter persistence, and recovery using
isolated fixtures before relying on a new installation.
