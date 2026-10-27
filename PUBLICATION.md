# Public source snapshot

This repository begins with a fresh source snapshot. It does not contain the
private deployment repository's commit history, review packages, runtime
databases, chat exports, models, credentials, or backup archives.

Deployment hostnames use example.com. IP examples use documentation-network
addresses. Filesystem locations are generic examples requiring adaptation.
The Compose defaults are included in this repository.

Validation completed for this snapshot:

- Core tests passed in an isolated container; the opt-in live test was skipped.
- Eighteen OAuth gateway and snapshot tests passed using synthetic fixtures.
- Core and optional-gateway Compose configurations passed config validation.
- Python sources parsed successfully.
- A signature and identifier scan found no original host domain, username,
  private host addresses, credential artifacts, or recognized secret tokens.

This is not a guarantee that every possible secret pattern is detectable.
The examples have not been deployed as a new installation. Project source is licensed under Apache-2.0; dependencies and models retain
their own licenses.
