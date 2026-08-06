# Discord community: setup and onboarding

Operational plan for boxxkite's Discord server. Written for a **solo
maintainer** (`.github/CODEOWNERS` is one person) — every recommendation here
is scoped to what one person can sustainably read, answer, and moderate.

Three facts drive every decision below:

1. **Solo maintenance.** Community-building advice written for 5-15 maintainer
   projects will drown you. Aggressive minimalism is correct at this stage.
2. **This project executes arbitrary untrusted code.** A working sandbox-escape
   PoC posted in a public channel is a zero-day handed to every self-hoster,
   with no patch available. This is the highest-stakes item on the page, and
   it's why there is deliberately no `#security` channel.
3. **The old invite already died in places that cannot be edited.** The
   previous `discord.gg` link expired while embedded in READMEs published to
   PyPI, npm, crates.io, and pkg.go.dev. Those published versions are frozen
   forever.

## 1. The invite link — never publish a `discord.gg` URL again

**Publish `https://boxxkite.com/discord`, a redirect the project controls.**

The repo now uses this everywhere (13 references across 11 files). A raw
Discord invite fixes today's outage; a self-owned redirect fixes today's *and*
every future one — including inside artifacts that can no longer be edited.

| | raw `discord.gg/xyz` | `boxxkite.com/discord` |
|---|---|---|
| Can expire | yes (this already happened) | no |
| Revocable by Discord | yes | no |
| Fixable after publishing to PyPI/npm/crates.io | **no** | **yes — repoint the redirect** |
| Survives leaving Discord entirely | no | yes |
| Survives pausing invites during a raid | no | yes |

That third row is the entire argument.

**Setup:**

1. Create the invite from `#welcome-and-rules`: **Expire after: Never**,
   **Max uses: No limit**, **Grant temporary membership: OFF** (temporary
   membership kicks users when they disconnect — an easy, silent mistake).
2. Revoke every other existing invite so no half-dead link survives.
3. Add the redirect on the site as a **307**, not a 301 — 301s are cached hard
   by browsers and would defeat the repointability that is the whole purpose.
4. Cut a patch release so the registry landing pages (which render the *latest*
   version's README) carry the working link.

> **Cross-repo dependency:** boxxkite.com is not in this repository. The
> `/discord` redirect must exist **before or alongside** these README changes,
> or a dead Discord link is simply swapped for a dead site link.

A vanity `discord.gg/boxxkite` needs Level 3 boost (14 boosts) — out of reach
for a new server, and unnecessary: the redirect already gives a branded,
memorable URL. If you hit Level 3 later, claim it and repoint the redirect;
nothing published needs to change.

## 2. Channels — 11 channels, 5 categories

**The anti-pattern to avoid:** one channel per component. With 9 packages that
means 10+ channels each getting one message a week. A dead channel is worse
than no channel — it signals abandonment to exactly the evaluating-buyer
audience boxxkite targets, and it fragments what little traffic exists.

**The rule: one busy forum with tags, not ten quiet channels.**

### 📌 START HERE (read-only)

| Channel | Purpose |
|---|---|
| `#welcome-and-rules` | Rules Screening target. The canonical "don't post exploits here" statement. |
| `#announcements` | Releases and breaking changes. Announcement type, so other servers can follow it. |
| `#roles` | Self-assignable role buttons, kept out of `#welcome` so that stays short. |

### 💬 COMMUNITY

| Channel | Purpose |
|---|---|
| `#general` | Chatter, intros, "we're evaluating boxxkite vs E2B". Keeps `#help` for questions. |
| `#showcase` | What people built. Highest-ROI channel for a "build your own agent product" audience, and near-zero moderation cost. |

### 🛟 SUPPORT

| Channel | Purpose |
|---|---|
| `#help` | **Forum.** The core of the server. Tagged, not split into per-SDK channels. |
| `#self-hosting` | **Forum.** Split from `#help` on day one — K8s/Helm questions are long, environment-specific, and log-heavy, and would otherwise swamp everything else. |
| `#bugs-and-feedback` | A **triage funnel, not a tracker.** Confirmed bugs go to GitHub Issues. Prevents Discord becoming a shadow issue tracker to reconcile. |

`#help` tags: `sdk-python`, `sdk-js`, `sdk-go`, `sdk-rust`, `control-plane`,
`mcp-server`, `core-runtime`, `handoff-cli`, `bastion`, `tools-surface`,
`solved`.

`#self-hosting` tags: `kubernetes`, `helm`, `docker-compose`, `kind-local`,
`render`, `networking`, `storage`, `apple-silicon`, `solved`.

Require at least one tag per post. This gives you "all open Rust SDK questions"
as a filter — something a channel-per-SDK layout can't do — while keeping the
conversation in one visibly-alive place.

### 🛠 CONTRIBUTING

| Channel | Purpose |
|---|---|
| `#contributors` | DCO/`git commit -s` questions, "is this approach right before I write code" — CONTRIBUTING.md routes people here. Must not be buried in `#help`. |
| `#github-feed` | Read-only webhook firehose. Activity signal for free. |

### 🔒 PRIVATE

| Channel | Purpose |
|---|---|
| `#maintainers` | Mod-log and AutoMod alert target. Create it now so the permission structure exists before a second maintainer arrives. |

**Deliberately not created:** no `#security` channel (§5), no voice channels
(dead space that invites abuse), no `#off-topic`/`#memes` (pure moderation
liability at this size), no per-SDK channels (forum tags cover it).

That's five places needing human attention: `#help`, `#self-hosting`,
`#bugs-and-feedback`, `#contributors`, `#general`. That is the ceiling for one
person.

## 3. Roles

Discord shows only the colour of the highest *coloured* role, so keep coloured
roles few enough that colour means something.

| # | Role | Colour | Assignment | Notes |
|---|---|---|---|---|
| 1 | **Maintainer** | brand | manual | Administrator. |
| 2 | **Bot** | — | automatic | Keep **below** Maintainer so a compromised bot can't touch your role. |
| 3 | **Moderator** | orange | manual | `Manage Messages`, timeout, `Manage Threads`, `Kick`. **Create it empty**; grant when a trusted regular emerges. Not `Ban`, not `Manage Roles`, at first. |
| 4 | **Core Contributor** | green | manual | Grant on first merged PR. Cosmetic, and the single most effective retention lever in small OSS communities. |
| 5 | **Startup / Elevated Access** | gold | manual, after vetting | The README's promise. Unlocks a private forum. |
| 6 | **Member** | none | **automatic on onboarding** | The security boundary — see below. |
| 7 | interest roles | none | self-assignable | Pingable cohorts only. |

### Member is the security boundary

Set **@everyone to no send permissions anywhere.** Attach every posting
permission to **Member**, granted automatically only on completing Rules
Screening + Onboarding.

- @everyone: `View Channels` on START HERE only. No send, no threads, no
  attachments, no embeds, no reactions.
- Member: send / attach / embed / react / create threads in the community,
  support, and contributing categories.

This means **a drive-by account cannot post until it has scrolled past the
security rule**, and it eliminates most spam-bot raids, which target servers
where @everyone can post on join. Pair with Verification Level **Medium**.

### Interest roles

`@sdk-python` · `@sdk-js` · `@sdk-go` · `@sdk-rust` · `@mcp` · `@self-hoster` ·
`@contributor-interest` · `@release-notifications`

Their purpose is narrow: ping a small relevant cohort on a breaking change
instead of `@everyone`. **Turn OFF "Allow anyone to @mention this role" on
every one** — a commonly-missed setting; without it any member can ping every
Rust user at 3am.

Not interest roles: `@core-runtime`, `@sidecar`, `@control-plane`, `@bastion`.
Nobody self-identifies as a "sidecar person," and unused roles are clutter.

### The Startup / Elevated Access tier

The README promises: *"discuss elevated access if you're a startup (dedicated
thread for that once you're in)."* Honour it literally:

- A **private forum** `#elevated-access`, visible to Maintainer + the role.
- Intake is not a public channel. `#roles` carries one entry point: "Request
  elevated access / usage-limit bump." That creates a **private thread** with
  just them and you — literally the promised dedicated thread.
- Grant the role at thread creation. Tag threads `evaluating` / `active` /
  `closed` for a lightweight CRM with no CRM.

Zero-effort day-one version: `#roles` says "DM the maintainer with your company
and use case," and you create the thread by hand. Fine at current scale.

### Permission hygiene

Nobody but Maintainer gets `Manage Roles`, `Manage Channels`, `Manage
Webhooks`, or `Administrator`. `Manage Webhooks` is an underrated escalation
path — a webhook can impersonate any name and avatar, including yours, in a
channel where members trust announcements. Turn off `Mention @everyone` for
every role, and require 2FA for moderation actions.

## 4. Onboarding

### The flow

1. Click `boxxkite.com/discord`.
2. **Rules Screening** — must scroll and click "I agree". Cannot see or post
   anything before this. The security rule gets read here.
3. **Onboarding** — 3 questions; answers assign interest roles and customise
   which channels they see first.
4. **Member role granted automatically.** They can now post.
5. Land in `#welcome-and-rules`, pushed onward to `#help` / `#self-hosting`.
6. One-line welcome in `#general`.

No bot-DM onboarding: many users have DMs from non-friends closed by default,
so DM-based onboarding silently fails for a meaningful fraction of joiners.
Native Onboarding has no such failure mode.

### `#welcome-and-rules` draft

> **boxxkite** — a self-hostable, Kubernetes-native sandbox for AI-agent code
> execution.
>
> Most agent-sandbox projects give you raw isolation and leave you to build the
> tool surface. boxxkite is the other half: a complete `bash`/`python`/file/
> search/process tool surface inside real Kubernetes pod isolation, Apache-2.0,
> self-hostable end to end.
>
> **This server is for teams building their own agent products** who need
> isolated, multi-tenant code execution at scale. If you just want your local
> coding assistant to run shell commands on your laptop, boxxkite is the wrong
> layer — we'll say so kindly in `#general`.
>
> **Rules**
>
> **1. 🔴 Never post security vulnerabilities here.** boxxkite executes
> arbitrary, untrusted, agent-generated code. A sandbox escape, network-isolation
> bypass, auth bypass, or credential leak posted here is a zero-day handed to
> everyone running boxxkite in production, with no patch available.
> **Report privately:** GitHub → Security → Report a vulnerability. See
> SECURITY.md.
> Not sure whether it qualifies? **Assume it does.** We will never be annoyed by
> a false positive. Exploit details posted publicly get deleted on sight — not
> because you did anything malicious, but because the message can't be un-read.
>
> **2. Be respectful, assume good faith, keep it technical.** We follow the
> Contributor Covenant. Report CoC issues via the same private GitHub path.
>
> **3. Ask in public, in the right forum.** `#help` for SDK/API/tooling,
> `#self-hosting` for Kubernetes and deployment. Tag your post. Please don't DM
> the maintainer for support — public answers help the next person.
>
> **4. No spam, recruiting, or unsolicited promotion.** Built something on
> boxxkite? `#showcase` is exactly for that.
>
> **5. This is a solo-maintained project.** Answers may take a day or two.
> Please be patient, and please help each other — it genuinely matters here.
>
> By clicking **I agree**, you're confirming you've read rule 1.

That last line converts "we told them" into "they affirmed it" — which is what
you want when you later delete someone's post.

### Onboarding questions

**Q1 — "What brings you to boxxkite?"** (single select, drives channel visibility)

| Option | Surfaces |
|---|---|
| 🔍 Evaluating boxxkite | `#general`, `#help`, `#showcase` |
| 🏗 Building on it | `#help`, `#showcase`, `#announcements` |
| ⚙️ Self-hosting it | `#self-hosting`, `#help` |
| 🛠 Contributing | `#contributors`, `#github-feed` |
| 👀 Just looking around | `#general`, `#showcase` |

**Q2 — "Which parts do you use?"** (multi-select, assigns roles)

🐍 Python SDK · 📜 JS/TS SDK · 🐹 Go SDK · 🦀 Rust SDK · 🔌 MCP server ·
☸️ Self-hosting on Kubernetes · 🤷 Not sure yet

**Q3 — "Want notifications?"** (multi-select, optional)

📣 New releases (`@release-notifications`) · 🌱 Good-first-issues
(`@contributor-interest`)

Stop at three. Every extra question measurably reduces completion — and
completion is what grants the Member role that lets them post.

### `#general` welcome

> 👋 Welcome {user}! Grab your SDK roles in `#roles`, ask anything in `#help`
> (or `#self-hosting` for K8s), and tell us what you're building.

One line, one mention. A wall of text reads as bot noise.

### Pinned posts for day one

These pre-answer questions the repo's own docs guarantee will arrive:

- `#self-hosting` — **Apple Silicon / arm64:** use `boxxkite-sandbox-minimal`.
  The full image is amd64-only; `deploy/sandbox.Dockerfile` hard-fails on arm64
  because the pinned Chrome-for-Testing build has no arm64 artifact.
- `#self-hosting` — **The Helm chart does not deploy the control-plane.** It
  has no Deployment/Service; sandbox pods are created programmatically at
  runtime. Guaranteed recurring confusion.
- `#self-hosting` — local `kind` setup, pointing at `deploy/local-kind/README.md`.
- `#help` — **Read before posting:** which package, which version, kubectl vs
  docker-compose, redacted logs, and "if it looks like a security issue, don't
  post it."
- `#contributors` — **CI is currently disabled**, so run the checks yourself:
  the package's own `pytest`, plus
  `ruff check src/ tests/ control-plane/src/ control-plane/tests/` and
  `pip-audit`. Also: DCO (`git commit -s`), and `bastion/`/`handoff-cli/` have
  no CI job at all.
- `#bugs-and-feedback` — confirmed bugs go to GitHub Issues
  (`blank_issues_enabled: false`, so a template is required).

## 5. Security policy for the server

The threat is concrete: someone finds a real escape, gets excited, and posts
"here's how I broke out of the pod, PoC attached" in `#help`. Deletion does not
undo it — Discord history is searchable and screenshot-able.

**Design principle: make the private path the only obvious path, and give the
public path nowhere to land.**

1. **Zero vuln-intake surface in Discord.** No `#security` channel, no security
   forum tag, no "report a vulnerability" affordance pointing anywhere inside
   Discord. Every security path leads out to GitHub private advisories —
   matching SECURITY.md, which correctly never mentions Discord.
2. **Security is rule #1**, affirmed by the Rules Screening click.
3. **AutoMod tripwire, alert-only.** A custom keyword rule on terms that
   co-occur with escape reports — `sandbox escape`, `container escape`,
   `privesc`, `0day`, `RCE`, `169.254.169.254`, `metadata endpoint`, `nsenter`,
   `X-Sidecar-Auth-Token`, `NetworkPolicy bypass`, `auth bypass`,
   `path traversal`, `cross-tenant` — routed to `#maintainers`.
   **Alert, do not block.** `nsenter` and `NetworkPolicy` are everyday
   vocabulary in this project; blocking would false-positive constantly. The
   point is to page *you* within minutes. Enable mobile push for `#maintainers`
   only. This is the single highest-value automation on this page.
4. **Pinned notice in `#help` and `#self-hosting`** mirroring SECURITY.md's own
   fallback: if you can't use GitHub private reporting, post only *"security
   issue, please contact me privately"* — no details, no repro, no logs.
5. **Pre-decided incident response**, so it isn't improvised under pressure:
   delete first (deletion is time-critical, apology isn't) → DM the reporter
   warmly and non-punitively → assess exposure → open the advisory yourself and
   credit them → if it was up long enough to be seen, treat it as disclosed and
   ship a `#announcements` mitigation note even before a fix.
   **Do not ban the reporter.** Banning a researcher for enthusiasm guarantees
   the next one goes to Twitter instead.
6. **Guard the leak path out of GitHub.** Never subscribe a Discord webhook to
   `repository_vulnerability_alert` or advisory events — an easy checkbox to
   tick by accident when selecting "send me everything."

## 6. Automation

**Use Discord native. You do not need Carl-bot or MEE6 on day one.**

| Need | Solution | Bot? |
|---|---|---|
| Rules agreement gate | Rules Screening | no |
| Roles on join | Onboarding | no |
| Spam / raid protection | AutoMod + Verification Level + Pause Invites | no |
| Welcome message | native system message | no |
| Levels / XP | — | **actively harmful** |

The one genuine gap: a member who joined before you added a role has no native
way to grab it later. If that actually happens, install **Carl-bot** for that
one job — button roles in `#roles`, minimal scopes, role placed below
Maintainer. It also gives you a mod log. Don't enable its automod (redundant),
levelling, or music.

**MEE6 is not worth it here.** Its value proposition is levelling, which turns
a technical server into a grind-for-rank server, and a nontrivial fraction of
this audience will judge the project for using it.

**AutoMod:** enable the spam / mention-spam / harmful-links presets, plus the
alert-only security rule from §5. Exempt Maintainer and Moderator, and exempt
`#maintainers` from the security rule or it will alert on itself.

### GitHub → Discord

Use Discord's built-in GitHub webhook (append `/github` to the webhook URL).
Two webhooks with **different** filters:

- **`#announcements`** — `Releases` only. Nothing else, or the channel stops
  being followable. Post one human sentence alongside each release; bot output
  alone reads as noise.
- **`#github-feed`** — issues, PRs, issue comments, PR reviews, optionally
  stars. **Exclude `Push`, `Workflow runs`, `Check runs`** — push events on a
  9-package monorepo will flood it, and CI is disabled anyway. Default
  notification setting: Nothing.

## 7. Rollout

### Phase 0 — before anyone is invited (~2-3 hours)

1. Permanent invite; revoke all others; temporary membership OFF.
2. `boxxkite.com/discord` → 307 redirect. **Blocking dependency.**
3. @everyone no-send; create Member; Verification Medium; require 2FA for mod.
4. Create the 11 channels and 5 categories; set read-only where noted.
5. Rules Screening, security first. Turn it on.
6. Onboarding: 3 questions, role grants, channel highlights. Require completion.
7. AutoMod presets + the alert-only security rule → `#maintainers` + mobile push.
8. Pinned security notice in `#help` and `#self-hosting`.
9. The other pinned posts from §4.
10. Create all roles; verify @mention is OFF on every interest role.

**Do not skip 5, 7, or 8** — that's the entire security posture, and it's 30
minutes of work.

### Phase 1 — open the door (same week)

11. Merge the link change (done in this PR) and confirm the redirect resolves.
12. Wire the two webhooks; confirm no advisory events are subscribed.
13. Cut a patch release so the registry pages carry the live link.
14. Seed 3-5 real answered threads in `#help`. An empty forum gets no first
    poster; one with five answered threads does.
15. Announce.

### Phase 2 — first 30 days (reactive)

16. Grant Core Contributor to the first merged-PR author, visibly and promptly.
17. Handle the first elevated-access request by hand. Automate only after 3+.
18. Install Carl-bot only if someone actually asks for a missed role.
19. Watch tag volume. Promote a tag to its own forum only after it exceeds ~10
    threads/week for two consecutive weeks.
20. Monthly stale-thread pass; mark solved threads.

### Phase 3 — only when numbers justify it

Moderator grant (after 4+ weeks of someone helping unprompted, starting with
`Manage Messages` + timeout only) · vanity URL at 14 boosts (then just repoint
the redirect) · office hours if requested.

### The trap to resist

The strongest temptation over the next three months will be adding channels
because the server feels quiet. **A quiet server with 5 channels reads as
focused; a quiet server with 20 reads as abandoned.** Growth here comes from
forum tags and pinned answers, not new channels. Let a specific tag's volume —
not a vibe — be the thing that forces a split.
