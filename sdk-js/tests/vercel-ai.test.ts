import assert from "node:assert/strict";
import { test } from "node:test";

import { BoxkiteClient } from "../src/index.js";
import { createSandboxTools } from "../src/vercel-ai.js";

function fakeFetch(handler: (req: { method: string; url: string; body: unknown }) => Response): typeof fetch {
  return (async (input: any, init?: any) => {
    const url = typeof input === "string" ? input : input.toString();
    const body = init?.body ? JSON.parse(init.body as string) : undefined;
    return handler({ method: init?.method ?? "GET", url, body });
  }) as typeof fetch;
}

function clientWith(handler: (req: { method: string; url: string; body: unknown }) => Response): BoxkiteClient {
  return new BoxkiteClient({
    baseUrl: "https://cp.example.com",
    apiKey: "bxk_live_test",
    fetchImpl: fakeFetch(handler),
  });
}

// Vercel AI SDK's tool().execute expects a second "options" argument
// (toolCallId, messages, ...) -- boxkite's tools never read it, but it's
// part of the real call shape, so tests pass a minimal stand-in rather
// than omitting it.
const toolCallOptions = { toolCallId: "call-1", messages: [] } as any;

test("createSandboxTools returns twelve tools with expected names", () => {
  const client = clientWith(() => new Response("{}", { status: 200 }));
  const tools = createSandboxTools(client, "sess-1");

  assert.deepEqual(Object.keys(tools).sort(), [
    "bash_tool",
    "file_create",
    "get_process_output",
    "glob",
    "grep",
    "list_processes",
    "ls",
    "send_process_input",
    "start_process",
    "stop_process",
    "str_replace",
    "view",
  ]);
});

test("bash_tool invokes exec and returns stdout", async () => {
  const client = clientWith(({ url }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes/sess-1/exec");
    return new Response(JSON.stringify({ exit_code: 0, stdout: "hello\n", stderr: "" }), { status: 200 });
  });
  const tools = createSandboxTools(client, "sess-1");

  const result = await tools.bash_tool.execute!({ command: "echo hello" }, toolCallOptions);
  assert.match(String(result), /hello/);
});

test("view tool surfaces api errors as a string, not a throw", async () => {
  const client = clientWith(() =>
    new Response(JSON.stringify({ error: { code: "not_found", message: "Sandbox session not found" } }), {
      status: 404,
    }),
  );
  const tools = createSandboxTools(client, "sess-1");

  const result = await tools.view.execute!({ path: "x.txt" }, toolCallOptions);
  assert.match(String(result).toLowerCase(), /not found|error/);
});

test("ls tool invokes ls and lists entries", async () => {
  const client = clientWith(({ url }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes/sess-1/files/ls");
    return new Response(JSON.stringify({ entries: [{ path: "a.txt", is_dir: false }] }), { status: 200 });
  });
  const tools = createSandboxTools(client, "sess-1");

  const result = await tools.ls.execute!({ path: "/workspace" }, toolCallOptions);
  assert.match(String(result), /a\.txt/);
});

test("start_process tool invokes startProcess and returns the process id", async () => {
  const client = clientWith(({ url }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes/sess-1/processes");
    return new Response(JSON.stringify({ process_id: "proc_1", status: "running", started_at: "now" }), {
      status: 201,
    });
  });
  const tools = createSandboxTools(client, "sess-1");

  const result = await tools.start_process.execute!({ command: "sleep 5" }, toolCallOptions);
  assert.match(String(result), /proc_1/);
});

test("list_processes tool reports an empty list", async () => {
  const client = clientWith(() => new Response(JSON.stringify({ processes: [] }), { status: 200 }));
  const tools = createSandboxTools(client, "sess-1");

  const result = await tools.list_processes.execute!({}, toolCallOptions);
  assert.match(String(result).toLowerCase(), /no background processes/);
});

test("enableHttpRequestTool gates the http_request tool", () => {
  const client = clientWith(() => new Response("{}", { status: 200 }));

  const withoutFlag = createSandboxTools(client, "sess-1");
  assert.equal((withoutFlag as any).http_request, undefined);

  const withFlag = createSandboxTools(client, "sess-1", { enableHttpRequestTool: true });
  assert.ok((withFlag as any).http_request);
});
