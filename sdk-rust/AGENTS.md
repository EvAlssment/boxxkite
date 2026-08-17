# AGENTS.md — sdk-rust

Guide for an AI coding agent working in `sdk-rust/`. For DCO sign-off and
the fork/PR flow, see the repo root's [CONTRIBUTING.md](../CONTRIBUTING.md).

## Setup

```bash
cd sdk-rust
cargo build
```

## Test

```bash
cargo test
```

Mocks the control-plane with `wiremock` (a real local HTTP server, not a
trait-mock). `wiremock` is HTTP-only, so `takeover()`'s test hand-rolls a
WebSocket handshake directly over `tokio` TCP + `tokio-tungstenite` rather
than going through `wiremock` — follow that same pattern for any other
WebSocket-based method's tests, rather than trying to fit it through the
HTTP mock.

## Lint / format

```bash
cargo clippy --all-targets -- -D warnings
cargo fmt
```

No `clippy.toml`/`rustfmt.toml` overrides — defaults apply, and `clippy`
warnings are hard errors (`-D warnings`), not advisory.

## Conventions

- Deliberately does not invent SDK surface area ahead of the other three
  SDKs — see this crate's own `lib.rs`/`README.md` notes on filesystem
  snapshots as a live example: the control-plane routes existed before any
  SDK wrapped them, and this crate explicitly waited rather than shipping
  first. Check whether a new feature you're wrapping here is already
  covered (or already being added) in `sdk-python`/`sdk-js`/`sdk-go`
  before assuming this crate should lead.
- Hand-written against the control-plane's REST API (see
  `sdk-python/AGENTS.md`'s note on cross-SDK drift) — this crate's own
  README documents a real prior drift incident from hand-typing a
  response model (`Webhook`'s newer fields) as a cautionary precedent;
  double-check a new/changed response type's exact shape against
  `control-plane/src/control_plane/schemas.py` rather than assuming a
  sibling SDK's existing model is still accurate.
