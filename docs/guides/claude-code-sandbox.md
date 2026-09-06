# Claude Code sandbox

The runnable [`claude_code_sandbox`](../../examples/browser-desktop/claude_code_sandbox/)
example builds the dedicated Claude Code image, starts it through the local
runtime, and invokes Claude Code headlessly through `bash_tool`.

Read the example's prerequisite and verification sections before running it.
The image build and tool wiring have documented local checks, but a live
Claude Code run still depends on the required runtime and model credential.
The example also explains why the command uses a custom image and how its
credential-handling boundary differs from a local shell.
