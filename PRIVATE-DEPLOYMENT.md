# One checkout, local overrides

Keep reusable source tracked. Keep site-specific configuration in private/.
Git and Docker ignore private/; do not force-add it. Secrets belong outside
the checkout. Ignore rules do not erase tracked files or old Git history.

The root compose.yaml includes the core and optional HTTPS gateway files.
For a site-specific deployment, private/compose.include.yaml can include
each base file with its corresponding override:

```yaml
include:
  - path:
      - ../deploy/compose.yaml
      - ../private/compose.override.yaml
  - path:
      - ../deploy/oauth/compose.yaml
      - ../private/oauth.override.yaml
```

The parent stack includes this wrapper. Relative paths resolve from each
included base file. The HTTPS gateway requires the https-client profile.
Use `docker compose --profile https-client config --quiet` to validate.

private/runtime.json optionally supplies operator-script settings:
public_base_url, state_root, log_root, credentials_root, backups_root,
oauth_state_root, docker_compose_file, borg_backup_script, borg_config_file,
and borg_repository. These are paths/settings, never secret values.
Without this file the scripts use generic example defaults. Configure it
before running host preparation or backup scripts on a real installation.

Set PUBLIC_BASE_URL in the private gateway override to your HTTPS origin.
The gateway derives its host/origin allowlist from that explicit setting.
It does not trust incoming Host headers to choose its public identity.

Back up private/ separately with restricted access. Existing credentials,
database state, logs, and model directories stay outside Git. Review
`git ls-files private` and `git diff --cached` before every publication.
Do not merge old private history into a public repository.
