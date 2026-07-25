## What changed

<!-- One or two sentences describing the change. -->

## Why

<!-- The problem this solves, or the issue it addresses (link it if one exists). -->

## How this was tested

<!-- Commands you ran, what you exercised manually, what's still untested. -->

## Checklist

- [ ] I ran the test suite (`pytest tests/` at root, and `pytest tests/` in
      `control-plane/` if this touches `control-plane/`) and it passes.
- [ ] I did not introduce billing/pricing language into API responses or
      user-facing copy (see `control-plane/tests/test_usage_limits.py`'s
      `_assert_no_pricing_language` — boxkite is a self-hosted sandbox, not a
      priced product).
- [ ] If this touches sidecar authentication, network policy, or Linux
      capabilities, I've read [SECURITY.md](../SECURITY.md)'s "What's in
      scope" section and understand this change gets extra review before
      merge (per [CONTRIBUTING.md](../CONTRIBUTING.md)).
- [ ] Commits are signed off (`git commit -s`) per the DCO in
      [CONTRIBUTING.md](../CONTRIBUTING.md).
