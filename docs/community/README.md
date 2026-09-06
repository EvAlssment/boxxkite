# Community

Use these links for project participation and project history:

- [Contributing](../../CONTRIBUTING.md) — development setup, DCO sign-off,
  fork/branch workflow, and pull-request expectations.
- [Governance](../../GOVERNANCE.md) — decision-making and maintainer roles.
- [Code of Conduct](../../CODE_OF_CONDUCT.md) — expectations and private
  enforcement route.
- [Security policy](../../SECURITY.md) — private vulnerability reporting and
  disclosed limitations. Do not put exploit details in a public issue.
- [Roadmap](../../ROADMAP.md) — proposed work and known gaps.
- [Changelog](../../CHANGELOG.md) — shipped changes.
- [Discord](https://discord.gg/JntfAx7cg5) — questions, implementation
  discussion, and community support.

## Before opening an issue

For a bug, include the smallest reproduction, the runtime mode, the relevant
package/version, and the command or API request that failed. For a security
issue, use the private path in [SECURITY.md](../../SECURITY.md) instead.

For a substantial design change, read [Governance](../../GOVERNANCE.md) and
open an issue before implementing it so the trade-offs can be discussed in
the open.

## Releases and verification

The root [README](../../README.md) lists the published package and image
registries. Image provenance and the current verification caveat are
documented in [SECURITY.md](../../SECURITY.md#verifying-released-images).
Check the exact tag and digest you intend to run; do not infer provenance
from a mutable image tag alone.
