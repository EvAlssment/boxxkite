# Foundry/Anvil smart-contract-audit sandbox

Closes [GitHub issue #137](https://github.com/EvAlssment/boxkite/issues/137):
boxkite had no crypto/web3 vertical use case and no Solidity/EVM toolchain in
its preset packages. This example packages Foundry (`forge`/`anvil`/`cast`)
into a boxkite sandbox image, wires up a deterministic local Anvil chain, and
demonstrates a toy "detect / patch / exploit" workflow against it -- while
staying **network-dark by default**: this example deliberately does not
grant the sandbox any live mainnet/testnet RPC egress, and never handles a
real private key. See "No live RPC egress, and why" and "Why real wallet/key
handling is explicitly out of scope" below.

## Why this use case (EVMbench)

OpenAI + Paradigm's **EVMbench** (arXiv 2603.04915) validates "smart
contract audit agent" as a real, fast-moving benchmark category: it scores
agents on **detect** (find a vulnerability), **patch** (fix it), and
**exploit** (prove a vulnerability is real by draining funds through it)
tasks, run against Foundry/Anvil sandboxes with pre-funded deterministic
accounts, plus a "veto proxy" that blocks admin/sensitive RPC calls so an
agent can't accidentally (or intentionally) do something irreversible to a
real chain. Reported results show the best models going from under 20% to
over 70% exploit success on real Code4rena bugs in under two years -- a
natural sibling to boxkite's existing quant-research use case
(kept-alive `python_interpreter` state fits the same "iterate against a live
environment across many tool calls" shape).

This example is a small, worked illustration of that shape, not a
reimplementation of EVMbench itself: instead of a real historical Code4rena
bug, `contracts/src/Vault.sol` is a deliberately toy, textbook reentrancy
vulnerability (an external `.call` before the internal balance is zeroed --
the same bug class as the 2016 DAO hack), with `contracts/src/VaultFixed.sol`
as its patched counterpart and `contracts/src/Attacker.sol` as the exploit.
`contracts/test/Exploit.t.sol` runs the identical attack against both and
shows one succeeds and the other fails closed --
`scripts/deploy_and_exploit.sh` replays the same attack against a live,
persistent local Anvil chain instead of forge's in-memory test EVM, which is
the more realistic shape of how an actual audit agent interacts with a
sandbox (deploy once, then run many separate commands against a chain that
keeps its state across them).

## What's in this directory

```
foundry_audit_sandbox/
├── Dockerfile                    # boxkite-minimal + git + Foundry v1.0.0, network-dark at runtime
├── docker-compose.override.yml   # points deploy/docker-compose.yml's sandbox service at this Dockerfile
├── contracts/
│   ├── foundry.toml
│   ├── src/Vault.sol              # toy vulnerable contract (reentrancy)
│   ├── src/VaultFixed.sol         # patched counterpart
│   ├── src/Attacker.sol           # exploit contract
│   └── test/Exploit.t.sol         # forge test: exploit succeeds vs. Vault, fails vs. VaultFixed
├── scripts/deploy_and_exploit.sh  # same attack, replayed against a live local Anvil chain
└── run_audit_sandbox.py           # scripted boxkite walkthrough (no LLM) tying it together
```

## Foundry's install method, verified before writing the Dockerfile

Checked directly against `getfoundry.sh`'s current installation docs before
writing anything, rather than assumed from training data:

- Foundry's own docs state plainly: **"Foundry no longer publishes npm
  packages for forge, cast, anvil, or chisel."** There is no apt/apk/pip
  package either -- its only supported install paths are the `foundryup`
  installer script, GitHub release tarballs, Docker, or building from source
  via `cargo`.
- The installer: `curl -L https://foundry.paradigm.xyz | bash` (installs
  `foundryup` itself), then `foundryup --install <version>` to fetch
  `forge`/`cast`/`anvil`/`chisel`. `foundryup --install v1.2.3` pins an exact
  released version (also accepts `latest`, `nightly`, `nightly-<sha>`); a
  released version's binaries are SHA-verified by default (`-f`/`--force`
  skips that verification -- deliberately never passed in this Dockerfile).
- Verified for real in this environment, not just read about: built a Wolfi
  (`cgr.dev/chainguard/wolfi-base`) container, ran the installer, and pinned
  `foundryup --install v1.0.0` -- `forge --version`/`anvil
  --version`/`cast --version` all reported `1.0.0-v1.0.0` afterward.

Because there is genuinely no pinned-package form of Foundry, this is a
**hand-maintained Dockerfile**, not a `docs/DECLARATIVE-BUILDER-DESIGN.md`
image (`POST /v1/images`) -- see the Dockerfile's own header comment for the
full reasoning. This is the same class of gap
`deploy/sandbox-claude-code.Dockerfile` already documents for
npm-global-installing Claude Code before `npm_packages` existed on
`SandboxImageBuildRequest`: a real, narrower limitation of what the
declarative builder's pinned `apt_packages`/`python_packages`/`npm_packages`
fields can express for this specific tool, not a workaround of it. The
declarative builder deliberately has no raw-command/Dockerfile-passthrough
escape hatch at all (see that doc's section 5), so there is no path to
express "run this installer script" through `POST /v1/images` today.

## The deterministic local chain

`scripts/deploy_and_exploit.sh` starts Anvil with:

```
anvil --chain-id 31337 \
  --mnemonic "test test test test test test test test test test test junk" \
  --accounts 5 --balance 1000 --host 127.0.0.1 --port 8545
```

- **Fixed chain ID** (`31337`, the long-standing Ganache/Hardhat/Anvil
  convention for a local dev chain).
- **Fixed, well-known mnemonic** -- the same one Hardhat's own test network
  defaults to. Anvil derives the exact same 5 accounts and private keys from
  it every single run:

  | # | Address | Private key |
  |---|---|---|
  | 0 | `0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266` | `0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80` |
  | 1 | `0x70997970C51812dc3A010C7d01b50e0d17dc79C8` | `0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d` |

  (Verified directly: started Anvil with this exact mnemonic and confirmed
  these addresses/keys, not copied from a training-data assumption.)

- **Pre-funded** -- 5 accounts, 1000 ETH each, on this chain only.

### Why these private keys are safe to print in this README

They are not secrets, and never were. This exact mnemonic is public,
well-known Ethereum tooling convention (Hardhat ships it as its own default
test network mnemonic) -- printing it here reveals nothing an attacker
doesn't already know, and it derives keys that hold no value on any chain
that matters: the accounts it produces are worthless by construction on
every real network, because everyone -- including any attacker -- already
knows this mnemonic and these keys. **The one rule that actually matters:
never fund an address derived from this mnemonic on mainnet, or any chain
where the ETH means anything.** Anvil's own deterministic accounts exist
specifically so a local dev/test chain never needs a real key at all.

## No live RPC egress, and why

This sandbox is **network-dark except to the Anvil instance it starts
itself** -- no live mainnet/testnet RPC access, by design, not by omission:

- Anvil is never passed `--fork-url` (or `--fork-block-number`/
  `--fork-chain-id`) anywhere in this example. Without `--fork-url`, Anvil
  never makes an outbound RPC call to anything -- it is a fully local,
  from-genesis chain. There is no code path in this example that could reach
  a real network even if egress were allowed.
- Egress is denied anyway, as a second, independent layer: boxkite's default
  `deploy/network-policy.yaml` is default-deny egress (DNS only, plus
  whatever an operator has explicitly allowlisted -- see that file), and
  local docker-compose (`deploy/docker-compose.yml`) puts the sandbox
  container on its own `sandbox-internal` Docker network with no route to
  the internet at all. Anvil's own JSON-RPC traffic is loopback
  (`127.0.0.1:8545`, inside the same container/pod), which neither
  mechanism governs or needs to allow -- it's not egress at all.
- **Verified for real, not asserted:** built the exact `Dockerfile` in this
  directory and ran it with `--read-only --cap-drop ALL --user 1001:1001
  --network none` -- i.e. the same read-only-rootfs, non-root,
  capability-dropped posture `deploy/pod-template.yaml` gives a real sandbox
  pod, plus zero network devices at all (`--network none`, a strictly
  tighter constraint than the pod's own NetworkPolicy). The full
  build → test → deploy → exploit flow (`scripts/deploy_and_exploit.sh`)
  ran to completion and fully drained the vault, entirely over loopback,
  while a `curl` to a real host from inside that same container failed with
  `Could not resolve host` -- proving the "no live RPC path exists" claim
  above rather than just stating it.

## Why real wallet/key handling is explicitly out of scope (issue #138)

This example never asks the sandbox to hold, inject, or sign with a
**real** private key. Everything in `contracts/` and `scripts/` uses Anvil's
own public, deterministic test keys against Anvil's own local chain --
nothing here ever needs a live wallet at all.

That's a deliberate scope boundary, not an oversight:
[GitHub issue #138](https://github.com/EvAlssment/boxkite/issues/138)
tracks the real, harder problem this example does **not** attempt to
solve -- how a sandbox would safely hand out or use a *real* private key
(mainnet or real testnet) for an agent to sign live transactions with.
Verified directly against the current code (not assumed): `bash_tool`/
`/exec` has no secret-injection path into a spawned process's environment at
all beyond the existing, separate `secret_env` opt-in
(`docs/SECRETS-DESIGN.md`), and `ExecRequest`'s `env` field was deliberately
removed as an exploitable leak path. There is today no grant type that
distinguishes a disposable testnet key from a real, value-bearing mainnet
key, no spend-cap or session-scoped-signing mechanism, and no trust-tier
model at all -- issue #138 scopes a dedicated design doc for exactly that,
with the same rigor the original secrets broker design went through. Until
that lands, this example's network-dark, local-chain-only, public-test-key
design is the correct, honest boundary: a real audit agent that also needs
to interact with a live chain (submit a bug-bounty PoC transaction, check a
real contract's on-chain state) is out of scope here by construction, not
silently unsupported.

## Why one `bash_tool` call, not `start_process` + many calls

The natural-looking design is: `start_process` a long-lived Anvil once,
then make many separate `bash_tool` calls against it as an agent explores
(deploy, call, redeploy a patched version, call again). **This does not
work by default in `RUNTIME_MODE=k8s`**, and this example does not paper
over that:

- Every `/exec` call (`bash_tool`) and every background process
  (`start_process`) runs inside a **fresh, empty network namespace** in K8s
  mode by default (`SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED=true`, `unshare
  -n` before `nsenter` -- see `sidecar/sidecar_execution.py`'s
  `build_k8s_exec_command`). `src/boxkite/tools/process_tools.py`'s own
  module docstring states this as an explicit non-goal: *"a backgrounded
  process's listening sockets are not reachable from any other tool call."*
  A `start_process`-launched Anvil would be exactly as unreachable from a
  later `bash_tool`'s `cast`/`forge` call as any other background process.
  `docs/NETWORK-INGRESS-DESIGN.md`'s `expose_port` mechanism does not help
  here either -- it only makes a process reachable from the **sidecar
  container itself** (for the preview-URL proxy), not from other `/exec`
  calls, which still get their own separate, isolated namespace regardless.
- **This example's approach:** `scripts/deploy_and_exploit.sh` starts Anvil,
  deploys, funds, attacks, and tears Anvil down again, all inside **one**
  `bash_tool` call (one shell, one process tree, one network namespace).
  This is fully portable -- it works identically in K8s mode and
  docker-compose mode, needs no operator-side tradeoff, and was the
  configuration actually verified end to end (see above).
- **If a genuinely persistent chain across many separate tool calls is
  wanted** (so an agent can iterate: deploy, inspect, patch, redeploy, retry
  -- across many turns, not one shell script), the real option is
  `SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED=false` for that deployment/pod,
  which removes the per-exec namespace isolation for **every** command in
  that session (not just Anvil-related ones), falling back to the pod's own
  shared network namespace + `deploy/network-policy.yaml`'s NetworkPolicy as
  the only backstop. This is the same class of explicit, disclosed tradeoff
  `src/boxkite/tools/git_tools.py`'s own module docstring already makes for
  enabling `git push`/`git pull`, and the same reasoning applies here:
  "no live RPC egress" still holds either way (it's enforced by
  NetworkPolicy's default-deny egress and by Anvil never being passed
  `--fork-url`, not by the per-exec isolation feature), but every other
  command in that session also loses its own per-exec isolation, not just
  the Foundry-related ones. Only take this tradeoff deliberately, on a
  pod/session you are otherwise comfortable running that way -- it is not
  this example's default, and `run_audit_sandbox.py` does not need or use
  it (see above).
- **In `RUNTIME_MODE=compose`** (the default local dev target for every
  example in this cookbook), this whole question doesn't arise the same
  way: verified directly against the code
  (`sidecar/sidecar_execution.py`'s `exec_in_sandbox`), compose-mode `/exec`
  uses plain `docker exec` with no `unshare -n` step at all -- every command
  already shares the one sandbox container's network namespace. A
  `start_process`-launched Anvil would already be reachable from a later
  `bash_tool` call with zero configuration change in compose mode. This
  example still uses the single-call form throughout so its behavior is
  identical (and equally portable) in both runtime modes, not because
  compose mode requires it.

## Running it

### 1. Build the image and point a local stack at it

Compose mode reuses one already-running `sandbox` container for every
session (verified directly against `src/boxkite/manager.py`: in
`RUNTIME_MODE=compose`, `SandboxManager` never creates a per-session
container at all) -- so there is no per-session `SANDBOX_IMAGE`/`image_id`
swap the way real K8s mode has. Swapping the image compose-mode uses means
changing what `docker compose up --build` builds for the `sandbox` service,
which is exactly what `docker-compose.override.yml` does (see that file's
own comments for why an override file, not an env var):

`boxkite up` itself can't be used directly here -- verified directly against
`src/boxkite/cli/cmd_up.py`: it only accepts a single compose file (no
support for a second `-f` override) and generates+injects
`SIDECAR_AUTH_TOKEN` into the `docker compose up` subprocess's own
environment itself, not via a `.env` file docker compose would pick up on
its own. So do the same thing manually, from the repo root:

```bash
export SIDECAR_AUTH_TOKEN=$(openssl rand -hex 32)
docker compose \
  -f deploy/docker-compose.yml \
  -f examples/foundry_audit_sandbox/docker-compose.override.yml \
  up -d --build
```

(Verified for real: `docker build` from this directory succeeds standalone,
and `docker compose -f deploy/docker-compose.yml -f
examples/foundry_audit_sandbox/docker-compose.override.yml build sandbox`,
run from the repo root with `SIDECAR_AUTH_TOKEN` set, really does build this
Dockerfile as the compose stack's `sandbox` service. `SIDECAR_AUTH_TOKEN` is
the only variable `deploy/docker-compose.yml` actually requires -- every
other `${...}` in it has a default.)

For a real Kubernetes deployment instead, push the built image to your
cluster's registry and set `SANDBOX_IMAGE` on the manager/operator
deployment (`src/boxkite/manager.py`'s `_create_pod` reads it directly) --
see `docs/CONFIGURATION.md`.

### 2. Run the scripted walkthrough

Reuse the same `SIDECAR_AUTH_TOKEN` value from step 1 (the sidecar
container only accepts the token it was actually started with):

```bash
export SIDECAR_AUTH_TOKEN=<the same value exported in step 1>
export RUNTIME_MODE=compose SIDECAR_URL=http://localhost:8080
pip install -e ../..
python run_audit_sandbox.py
```

This seeds `/workspace` from the image's pre-baked template (contracts +
forge-std + a pre-warmed solc cache -- all fetched once at image-build time,
never at session runtime; see the Dockerfile's own comments for why they
live under `/opt/foundry-audit-template` and not `/workspace`), runs
`forge build`/`forge test` (the detect-and-verify-the-patch step), then runs
`scripts/deploy_and_exploit.sh` against a live Anvil chain (the exploit
step) -- printing each step's real output.

## What was verified, and how (be specific, not just "it works")

- **The Dockerfile builds successfully** and `forge`/`anvil`/`cast`/`chisel`
  all report `1.0.0-v1.0.0` afterward.
- **The full audit workflow runs correctly under the pod's actual
  constraints, not a looser stand-in:** `docker run --read-only --cap-drop
  ALL --user 1001:1001 --network none` against the built image --
  `forge build` (hits the pre-warmed solc cache, zero network calls),
  `forge test` (both tests pass: the exploit drains `Vault.sol`, and fails
  against `VaultFixed.sol`), and `scripts/deploy_and_exploit.sh` (deploys to
  a live Anvil chain, funds it, and the reentrancy attack fully drains it --
  attacker balance 11 ETH, vault balance 0, `reentryCount` 11) all completed
  successfully with `--network none` in effect. A `curl` to a real host
  from inside that same locked-down container failed with "Could not
  resolve host" -- confirming zero egress was actually possible, not merely
  unused.
- **A real, documented bug was found and fixed while verifying this
  example**, worth knowing if extending it: `cast send`'s default
  `eth_estimateGas`-derived gas limit under-estimates what a multi-level
  reentrant call chain needs, silently capping the "exploit" at one reentry
  (looks like a working defense; is actually a gas-estimation artifact of
  the calling transaction). `scripts/deploy_and_exploit.sh` passes an
  explicit, generous `--gas-limit` on the attack transaction specifically
  because of this -- see that script's own comment.
- **The `docker-compose.override.yml` merge was run for real**
  (`docker compose ... build sandbox` against the actual override file in
  this directory), not just written from documentation.
- **Not verified in this pass:** `run_audit_sandbox.py`'s `SandboxManager`
  wiring was checked against a fake manager only (syntax + tool-spec
  construction + a mocked `bash_tool` call), not against a live local
  `boxkite up` stack -- no full compose stack (sidecar + MinIO/Vault) was
  stood up in this environment. If you hit an issue running it against a
  real stack, please file it with the exact traceback.

## References

- [GitHub issue #137](https://github.com/EvAlssment/boxkite/issues/137) --
  this example's own tracking issue.
- [GitHub issue #138](https://github.com/EvAlssment/boxkite/issues/138) --
  wallet/private-key secrets design; the explicit reason real-key handling
  is out of scope here.
- `docs/DECLARATIVE-BUILDER-DESIGN.md` -- why this is a hand-maintained
  Dockerfile, not a `POST /v1/images` image.
- `docs/PROCESS-SESSIONS-DESIGN.md` / `src/boxkite/tools/process_tools.py`
  -- `start_process`'s documented network-isolation non-goal.
- `docs/NETWORK-INGRESS-DESIGN.md` -- why `expose_port` doesn't solve
  cross-`/exec`-call reachability either.
- `deploy/network-policy.yaml` -- the default-deny egress NetworkPolicy this
  example relies on as a second, independent layer.
- EVMbench, arXiv 2603.04915 -- the benchmark this example's use case is
  inspired by.
