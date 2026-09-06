# Declarative image builders

The control-plane image-builder API can layer pinned packages onto a base
image and produce a sandbox image without a hand-maintained Dockerfile. The
current runnable paths are:

- [Claude Code declarative builder](../../examples/browser-desktop/claude_code_declarative_builder/)
  for a Claude-Code-capable image; and
- [Quant research declarative builder](../../examples/storage/quant_research_declarative_builder/)
  for a pinned Python package set.

Both examples document their request shape, prerequisites, and what was
actually verified. In particular, a successful control-plane build request is
not by itself proof that a resulting image has been created and run in a live
Kubernetes sandbox; follow each example's verification boundary.
