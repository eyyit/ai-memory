# Prevent accidental publication

The local pre-push hook checks every commit reachable from the tips being
pushed. It checks historical filenames, optional private identifiers, and
Gitleaks' standard secret rules. Deleting a secret in a later commit does
not make the earlier commit safe. Pushes stop if a required tool or local
policy is missing. Findings are redacted; no report artifact is uploaded.

Ignored files can still be force-added. The guard also rejects private/,
secrets/, exports/, backups/, models/, data/, review directories, credential
files, database dumps and archives anywhere in pushed history. .env.example
is the only environment-file exception. Narrowly review any future exception.

For the home installation, keep deployment identifiers as regular-expression
strings under private/push-policy.json in a nonempty patterns list. Never
commit that file or actual secret values. Private rules run locally; CI
uses only the generic rules so site identifiers are not exposed publicly.

For Linux x86-64, install the checksum-pinned scanner and hook per checkout:

```sh
sh scripts/install-gitleaks.sh private/tools
chmod +x .githooks/pre-push
git config --local core.hooksPath .githooks
python3 scripts/check-push.py
```

Do not overwrite existing hooks without reviewing how to preserve them.
Hooks are not automatically installed by Git clone. Every additional
checkout must install the hook and scanner and create its local policy.
Other operating systems need an equivalent reviewed native scanner install.

GitHub Actions repeats generic history checks after pushes and on pull
requests, with read-only permissions and complete history checkout. That
workflow cannot prevent the initial upload. No private deployment settings
or credentials are supplied to Actions. It uses Gitleaks 8.30.1, not the
separately licensed Gitleaks Action, and pins the checkout action by SHA.

Enable GitHub Secret Protection and push protection in repository Settings,
Advanced Security. That can reject supported secrets before acceptance,
even if a local hook is absent. It does not enforce the local private-file
and site-identifier policy. Repository administrators can still bypass or
disable controls. Local hooks can be bypassed with --no-verify.

No hook can guarantee that arbitrary private text is detected. If a real
credential is uploaded, revoke/rotate it first and review history cleanup.
Do not simply silence a detection or add a blanket ignore rule.

The scanner rejects repository-local .gitleaksignore files even if they
are untracked, because Gitleaks also reads them from its source directory.
The shared administrator-owned ignore file has no exception fingerprints.

To roll back this installation, restore the previous local core.hooksPath
setting (or unset it if none existed). Remove only the reviewed guard files
and private scanner/policy additions. This needs no container restart.

## Shared installation for several users

An administrator can install scripts/global-hooks.py as one shared hook
dispatcher. Install check-push.py, Gitleaks and the Gitleaks configuration
under /usr/local/libexec/git-push-guard. Place hook-name symlinks under
/usr/local/share/git-push-guard/hooks, pointing to the dispatcher. Create
/etc/git-push-guard/repos.json as a map of checkout paths to private policy
file paths. Those deployment paths belong outside the public repository.

Enable that hook directory with git config --global core.hooksPath for each
intended account, rather than changing the system-wide Git configuration.
Each user gets generic secret checks in all repositories. Mapped checkouts
also get the strict private-file and identifier checks. Unmapped projects
can retain legitimate data/model/archive files. Native repository hooks are
forwarded through the dispatcher, including a native pre-push hook.

The dispatcher requires hook-name symlinks for all native hook events to
preserve them. A repo-local hooksPath overrides the user's global setting,
and command-line overrides or --no-verify can still bypass it. Future Git
hook event types require a matching dispatcher link. Bare repositories and
linked worktrees require installation tests before activation. This shared
installation is optional; the per-checkout approach above also works.

For a deliberate private backup repository, the shared dispatcher supports
private_backup_exceptions in its local configuration. Each exception pins
a checkout, GitHub owner/repository and numeric repository ID. It checks
Git's actual push URL, never just the remote name. An authenticated GitHub
metadata request must confirm the exact ID/name and private visibility on
every exempt push. A changed destination, public visibility, missing token,
redirect, lookup error or unpinned ID blocks that backup push. A 404 is not
proof of privacy. No other private repository is automatically exempted.

The shared github_metadata_token_file contains a fine-grained token limited
to metadata read access for the approved backup repository. Keep it outside
public source, readable only by the intended accounts. Native hooks still
run for an exempt backup push. This exception deliberately permits secrets
in the verified private backup; it does not make those secrets encrypted.
