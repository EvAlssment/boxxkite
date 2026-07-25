# boxkite-bastion

A standalone SSH bastion in front of boxkite's existing human-takeover
feature — real `ssh` from a local terminal, without a second `sshd` inside
the sandbox container. See
[`docs/SSH-BASTION-DESIGN.md`](https://github.com/EvAlssment/boxkite/blob/main/docs/SSH-BASTION-DESIGN.md)
for the full design rationale (GitHub issue #134) and `SECURITY.md` for
this component's trust boundary.

```
developer's ssh client
      |  real SSH protocol
      v
  bastion/  (this component -- public listener, NOT in the sandbox pod)
      |  outbound WS, ?token=<takeover token>  (same as the dashboard today)
      v
control-plane  WS /v1/sandboxes/{id}/takeover   (unchanged)
      |
      v
sidecar  WS /pty   (unchanged, nsenter-based PTY, non-root sandbox user)
```

`bastion/` never talks to the sidecar and never gains any capability
inside the sandbox — it is a protocol-translating client of
control-plane's existing `WS /takeover` route, exactly like the dashboard
and JS SDK, speaking SSH to the human instead of WebSocket + xterm.js.

## Usage

1. Mint a takeover token (requires an `"admin"`-role API key) — the exact
   same route and token the dashboard's takeover terminal uses:

   ```bash
   curl -X POST https://your-control-plane.example.com/v1/sandboxes/<session_id>/takeover-token \
     -H "Authorization: Bearer $BOXKITE_API_KEY"
   ```

   Returns `{"token": "...", "expires_at": "...", "read_only": false}`.
   The token is short-lived and single-use.

   > **TTL caveat (current reality vs. design):** the token uses
   > control-plane's existing `BOXKITE_TAKEOVER_TOKEN_TTL_SECONDS` (default
   > **30s**). The design doc (§3, step 2) proposes a separate, longer
   > `BOXKITE_BASTION_TOKEN_TTL_SECONDS` (~120s) and a `for_bastion` request
   > flag so a human has more time to paste the token as an SSH password,
   > but **that is not implemented on the control-plane side yet** — the
   > mint route (`mint_sandbox_takeover_token`) accepts neither today. Mint
   > the token and connect promptly, or track this as follow-up work. See
   > `docs/SSH-BASTION-SECURITY-REVIEW.md`.

2. Connect with a real `ssh` client, using the session_id as the username
   and the minted token as the password:

   ```bash
   ssh <session_id>@bastion.your-domain.example -p 2222
   # password: <the minted token>
   ```

## Running the bastion

```bash
pip install -e .
BOXKITE_BASTION_CONTROL_PLANE_URL=https://your-control-plane.example.com \
BOXKITE_BASTION_LISTEN_PORT=2222 \
boxkite-bastion
```

| Variable | Required | Meaning |
|---|---|---|
| `BOXKITE_BASTION_CONTROL_PLANE_URL` | yes | control-plane's base URL (`http(s)://` or `ws(s)://`) -- the ONLY network destination this process ever talks to |
| `BOXKITE_BASTION_LISTEN_HOST` | no (default `0.0.0.0`) | SSH listener bind address |
| `BOXKITE_BASTION_LISTEN_PORT` | no (default `2222`) | SSH listener port |
| `BOXKITE_BASTION_HOST_KEY_PATH` | no | path to a persistent SSH host key (PEM); an ephemeral key is generated per-process if unset, which changes across restarts |

## What this component does NOT do

- No public-key auth, no user/key database of its own — the takeover token
  IS the SSH password, and that's the only auth method offered.
- No `scp`/`sftp`/one-shot `ssh host <command>` — only an interactive shell
  session (`exec_requested`/`subsystem_requested` are both rejected). Both
  would need their own file-transfer/exec bridge to the sidecar's HTTP
  routes; out of scope here (see the design doc's "Before anyone builds
  this" section).
- No terminal resize forwarding (`pty-req`/`window-change` are accepted but
  not wired to a `TIOCSWINSZ` ioctl on the sidecar's PTY) — a real, known
  gap disclosed in the design doc section 4, not fixed by this component.
- No sidecar credentials, no cluster/API access, no volume mounts — this
  process's only outbound call is `WS .../takeover` on control-plane.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

`tests/test_auth_bridge.py` is the security-critical suite: the
`(username=session_id, password=token)` → `WS .../takeover?token=` exchange
that authentication for this whole component reduces to.
`tests/test_bridge.py` covers the asyncssh integration layer's own
byte-relay/ordering/cleanup logic with fakes.
`tests/test_integration_smoke.py` runs one real asyncssh client against a
real asyncssh server (only the outbound `websockets.connect` call is
faked) to catch real-wiring mistakes the fakes-only tests can't see.
