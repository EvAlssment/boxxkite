import assert from "node:assert/strict";
import { test } from "node:test";

import { BoxkiteClient } from "../src/index.js";
import { createSandboxTools } from "../src/langchain.js";

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

test("createSandboxTools returns twelve tools with expected names", () => {
  const client = clientWith(() => new Response("{}", { status: 200 }));
  const tools = createSandboxTools(client, "sess-1");

  const names = tools.map((t) => t.name).sort();
  assert.deepEqual(names, [
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
  const bashTool = tools.find((t) => t.name === "bash_tool")!;

  const result = await (bashTool as any).invoke({ command: "echo hello" });
  assert.match(String(result), /hello/);
});

test("view tool surfaces api errors as a string, not a throw", async () => {
  const client = clientWith(() =>
    new Response(JSON.stringify({ error: { code: "not_found", message: "Sandbox session not found" } }), {
      status: 404,
    }),
  );
  const tools = createSandboxTools(client, "sess-1");
  const view = tools.find((t) => t.name === "view")!;

  const result = await (view as any).invoke({ path: "x.txt" });
  assert.match(String(result).toLowerCase(), /not found|error/);
});

test("ls tool invokes ls and lists entries", async () => {
  const client = clientWith(({ url }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes/sess-1/files/ls");
    return new Response(JSON.stringify({ entries: [{ path: "a.txt", is_dir: false }] }), { status: 200 });
  });
  const tools = createSandboxTools(client, "sess-1");
  const lsTool = tools.find((t) => t.name === "ls")!;

  const result = await (lsTool as any).invoke({ path: "/workspace" });
  assert.match(String(result), /a\.txt/);
});

test("glob tool invokes glob and lists matches", async () => {
  const client = clientWith(({ url }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes/sess-1/files/glob");
    return new Response(JSON.stringify({ matches: [{ path: "a.py" }] }), { status: 200 });
  });
  const tools = createSandboxTools(client, "sess-1");
  const globTool = tools.find((t) => t.name === "glob")!;

  const result = await (globTool as any).invoke({ pattern: "**/*.py" });
  assert.match(String(result), /a\.py/);
});

test("grep tool invokes grep and lists matches", async () => {
  const client = clientWith(({ url }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes/sess-1/files/grep");
    return new Response(JSON.stringify({ matches: [{ path: "a.py", line: 1, text: "import os" }], truncated: false }), {
      status: 200,
    });
  });
  const tools = createSandboxTools(client, "sess-1");
  const grepTool = tools.find((t) => t.name === "grep")!;

  const result = await (grepTool as any).invoke({ pattern: "import os" });
  assert.match(String(result), /a\.py/);
});

test("grep tool surfaces reported error as a string", async () => {
  const client = clientWith(() => new Response(JSON.stringify({ matches: [], error: "invalid regex" }), { status: 200 }));
  const tools = createSandboxTools(client, "sess-1");
  const grepTool = tools.find((t) => t.name === "grep")!;

  const result = await (grepTool as any).invoke({ pattern: "(" });
  assert.match(String(result), /invalid regex/);
});

test("start_process tool invokes startProcess and returns the process id", async () => {
  const client = clientWith(({ url }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes/sess-1/processes");
    return new Response(JSON.stringify({ process_id: "proc_1", status: "running", started_at: "now" }), {
      status: 201,
    });
  });
  const tools = createSandboxTools(client, "sess-1");
  const startProcessTool = tools.find((t) => t.name === "start_process")!;

  const result = await (startProcessTool as any).invoke({ command: "sleep 5" });
  assert.match(String(result), /proc_1/);
});

test("get_process_output tool invokes getProcessOutput and returns the output", async () => {
  const client = clientWith(({ url }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes/sess-1/processes/proc_1/output");
    return new Response(
      JSON.stringify({ status: "exited", stdout_chunk: "hello", next_offset: 5, truncated: false, exit_code: 0 }),
      { status: 200 },
    );
  });
  const tools = createSandboxTools(client, "sess-1");
  const getProcessOutputTool = tools.find((t) => t.name === "get_process_output")!;

  const result = await (getProcessOutputTool as any).invoke({ processId: "proc_1" });
  assert.match(String(result), /hello/);
  assert.match(String(result), /exit_code: 0/);
});

test("stop_process tool invokes stopProcess and returns the final status", async () => {
  const client = clientWith(({ url }) => {
    assert.equal(new URL(url).pathname, "/v1/sandboxes/sess-1/processes/proc_1/stop");
    return new Response(JSON.stringify({ status: "stopped", exit_code: 143 }), { status: 200 });
  });
  const tools = createSandboxTools(client, "sess-1");
  const stopProcessTool = tools.find((t) => t.name === "stop_process")!;

  const result = await (stopProcessTool as any).invoke({ processId: "proc_1" });
  assert.match(String(result), /stopped/);
});

test("list_processes tool reports an empty list", async () => {
  const client = clientWith(() => new Response(JSON.stringify({ processes: [] }), { status: 200 }));
  const tools = createSandboxTools(client, "sess-1");
  const listProcessesTool = tools.find((t) => t.name === "list_processes")!;

  const result = await (listProcessesTool as any).invoke({});
  assert.match(String(result).toLowerCase(), /no background processes/);
});
