# Handoff adapters

`boxxkite handoff <tool>` moves a resumable local coding-agent session into a
fresh sandbox. It uses the tool's own portable credential and resumes the
tool's conversation rather than reducing the handoff to a task summary.

The detailed adapter contract, credential-handling rules, security incident
write-up, and current adapter table are in the [original handoff reference](../handoff-adapters.md).
The implementation lives under [`src/boxxkite/handoff/`](../../src/boxxkite/handoff/).

After a hosted handoff, install the [sandbox troubleshooting skill](troubleshoot-sandbox.md)
if the coding agent needs to investigate the session's runtime state.

In short, an adapter locates the local session files and validated resume
identifier; the shared orchestrator creates a sandbox, uploads the required
files, and opens the existing takeover channel. Credentials are uploaded via
an ephemeral file rather than typed as a literal into the takeover channel.

Before adding an adapter, confirm that the tool has a locally resumable
session format and a portable, scoped credential. If either is missing, the
adapter should say so rather than pretending that a fresh session is a full
handoff.
