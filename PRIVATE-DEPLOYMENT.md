# Keep local deployment settings beside public source

Public source and local-only settings can share one checkout. Keep reusable
code and safe examples tracked; put site-specific settings in private/.
Both .gitignore and .dockerignore exclude that directory. Never force-add it.

Example layout:

```text
ai-memory/
  src/                     tracked reusable code
  deploy/                  tracked example configuration
  private/                 untracked local-only configuration
    compose.yaml
    deployment.env
```

Keep secrets in an external secret directory when possible. Use the private
overlay for paths to those files, hostname settings, and local mount choices.
Do not bake secrets into images, put them in tool arguments, or print them
in logs. Back up local-only configuration separately with restricted access.

For a configured installation, merge the reviewed overlay explicitly:

```sh
docker compose -f compose.yaml -f private/compose.yaml config --quiet
```

Adapt all example paths and hostname allowlists first. Some deployment
examples include fixed source-level example hostnames and assumptions;
move those into configurable settings before using one public checkout as
the live installation. This document does not perform that migration.

Verify exclusions before adding real settings:

```sh
git check-ignore private/compose.yaml private/deployment.env
git ls-files private
```

The second command should print nothing. Git ignore rules do not remove
tracked files or old commits. Never merge a private deployment branch or
the old private repository history into a public repository merely because
its current files are ignored. Ignoring part of a tracked file is impossible.

Review staged changes and scan reachable history before publication. Ignore
rules are an accident-prevention measure, not encryption or access control.

The existing private live deployment remains separate until a migration is
reviewed. The clean public history can later become the common source base;
local-only overrides should remain untracked throughout that transition.
