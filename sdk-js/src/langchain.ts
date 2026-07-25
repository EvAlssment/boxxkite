/**
 * LangChain.js tool factory over a hosted boxkite control-plane.
 *
 * Mirrors the Python SDK's create_sandbox_tools (same twelve tool names:
 * bash_tool, file_create, view, str_replace, ls, glob, grep, start_process,
 * get_process_output, send_process_input, stop_process, list_processes; plus
 * an optional http_request tool) and boxkite.tools' own shapes, so switching
 * between a self-hosted deployment
 * and this hosted client needs no changes beyond which factory produced
 * the tools. Requires `@langchain/core` as a peer dependency -- import
 * from "boxkite-client/langchain", not the package root, so consumers who
 * don't use LangChain never need to install it.
 */

import { tool } from "@langchain/core/tools";
import { z } from "zod";

import { BoxkiteClient } from "./client.js";
import { BoxkiteApiError } from "./errors.js";

export function createSandboxTools(
  client: BoxkiteClient,
  sessionId: string,
  options?: { enableHttpRequestTool?: boolean },
) {
  const bashTool = tool(
    async ({ command }: { command: string }) => {
      try {
        const result = await client.exec(sessionId, command);
        if (result.exit_code !== 0) {
          return `Command exited ${result.exit_code}. stdout:\n${result.stdout}\nstderr:\n${result.stderr}`;
        }
        return result.stdout as string;
      } catch (err) {
        if (err instanceof BoxkiteApiError) return `Error running command: ${err.message}`;
        throw err;
      }
    },
    {
      name: "bash_tool",
      description: "Run a shell command in the sandbox. Returns stdout, or stderr and the exit code if the command failed.",
      schema: z.object({ command: z.string() }),
    },
  );

  const fileCreate = tool(
    async ({ path, content }: { path: string; content: string }) => {
      try {
        const result = await client.fileCreate(sessionId, path, content);
        return `Wrote ${result.path ?? path} (${result.size ?? content.length} bytes)`;
      } catch (err) {
        if (err instanceof BoxkiteApiError) return `Error creating file: ${err.message}`;
        throw err;
      }
    },
    {
      name: "file_create",
      description: "Create or overwrite a file in the sandbox workspace.",
      schema: z.object({ path: z.string(), content: z.string() }),
    },
  );

  const view = tool(
    async ({ path }: { path: string }) => {
      try {
        const result = await client.view(sessionId, path);
        return (result.content as string | undefined) ?? JSON.stringify(result);
      } catch (err) {
        if (err instanceof BoxkiteApiError) return `Error viewing ${path}: ${err.message}`;
        throw err;
      }
    },
    {
      name: "view",
      description: "View a file's contents, or list a directory's entries.",
      schema: z.object({ path: z.string() }),
    },
  );

  const strReplace = tool(
    async ({ path, old_str, new_str }: { path: string; old_str: string; new_str: string }) => {
      try {
        const result = await client.strReplace(sessionId, path, old_str, new_str);
        return `Replaced in ${result.path ?? path} (${result.occurrences ?? 1} replacement(s))`;
      } catch (err) {
        if (err instanceof BoxkiteApiError) return `Error editing ${path}: ${err.message}`;
        throw err;
      }
    },
    {
      name: "str_replace",
      description: "Replace a unique string in a sandbox file. old_str must appear exactly once in the file.",
      schema: z.object({ path: z.string(), old_str: z.string(), new_str: z.string() }),
    },
  );

  const lsTool = tool(
    async ({ path }: { path?: string }) => {
      try {
        const result = await client.ls(sessionId, { path });
        const entries = (result.entries as unknown[] | undefined) ?? [];
        if (entries.length === 0) return `${path ?? "/"} is empty.`;
        return entries.map((e) => JSON.stringify(e)).join("\n");
      } catch (err) {
        if (err instanceof BoxkiteApiError) return `Error listing ${path ?? "/"}: ${err.message}`;
        throw err;
      }
    },
    {
      name: "ls",
      description: "List the direct children of a directory in the sandbox workspace.",
      schema: z.object({ path: z.string().optional() }),
    },
  );

  const globTool = tool(
    async ({ pattern, path }: { pattern: string; path?: string }) => {
      try {
        const result = await client.glob(sessionId, pattern, { path });
        const matches = (result.matches as unknown[] | undefined) ?? [];
        if (matches.length === 0) return `No files matched ${pattern} under ${path ?? "/"}.`;
        return matches.map((m) => JSON.stringify(m)).join("\n");
      } catch (err) {
        if (err instanceof BoxkiteApiError) return `Error matching ${pattern}: ${err.message}`;
        throw err;
      }
    },
    {
      name: "glob",
      description: "Find files by name pattern (e.g. '**/*.py') under a directory in the sandbox workspace.",
      schema: z.object({ pattern: z.string(), path: z.string().optional() }),
    },
  );

  const grepTool = tool(
    async ({ pattern, path, glob, maxMatches }: { pattern: string; path?: string; glob?: string; maxMatches?: number }) => {
      try {
        const result = await client.grep(sessionId, pattern, { path, glob, maxMatches });
        if (result.error) return `Error searching for ${pattern}: ${result.error}`;
        const matches = (result.matches as unknown[] | undefined) ?? [];
        if (matches.length === 0) return `No matches for ${pattern} under ${path ?? "/"}.`;
        const suffix = result.truncated ? " (truncated)" : "";
        return matches.map((m) => JSON.stringify(m)).join("\n") + suffix;
      } catch (err) {
        if (err instanceof BoxkiteApiError) return `Error searching for ${pattern}: ${err.message}`;
        throw err;
      }
    },
    {
      name: "grep",
      description:
        "Search file contents by regex pattern under a directory in the sandbox workspace, optionally restricted to files matching a glob.",
      schema: z.object({
        pattern: z.string(),
        path: z.string().optional(),
        glob: z.string().optional(),
        maxMatches: z.number().optional(),
      }),
    },
  );

  const startProcessTool = tool(
    async ({
      command,
      description,
      maxRuntimeSeconds,
    }: {
      command: string;
      description?: string;
      maxRuntimeSeconds?: number;
    }) => {
      try {
        const result = await client.startProcess(sessionId, command, { description, maxRuntimeSeconds });
        return `Started process ${result.process_id} (status=${result.status}). Use get_process_output("${result.process_id}") to check on it.`;
      } catch (err) {
        if (err instanceof BoxkiteApiError) return `Error starting process: ${err.message}`;
        throw err;
      }
    },
    {
      name: "start_process",
      description:
        "Start a long-running background process in the sandbox (a dev server, a test watcher, a long build, a REPL) that keeps running after this tool call returns. Distinct from bash_tool, which is one-shot and bounded by its own timeout.",
      schema: z.object({
        command: z.string(),
        description: z.string().optional(),
        maxRuntimeSeconds: z.number().optional(),
      }),
    },
  );

  const getProcessOutputTool = tool(
    async ({ processId, sinceOffset }: { processId: string; sinceOffset?: number }) => {
      try {
        const result = await client.getProcessOutput(sessionId, processId, { sinceOffset });
        const lines = [`status: ${result.status}`];
        if (result.exit_code !== null && result.exit_code !== undefined) lines.push(`exit_code: ${result.exit_code}`);
        if (result.truncated) lines.push("(earlier output was truncated -- the buffer only keeps recent output)");
        lines.push(`next_offset: ${result.next_offset}`);
        lines.push("--- output ---");
        lines.push(result.stdout_chunk || "(no new output)");
        return lines.join("\n");
      } catch (err) {
        if (err instanceof BoxkiteApiError) return `Error getting process output: ${err.message}`;
        throw err;
      }
    },
    {
      name: "get_process_output",
      description:
        "Poll a background process's output since a given byte offset. Call repeatedly to watch a process's progress.",
      schema: z.object({ processId: z.string(), sinceOffset: z.number().optional() }),
    },
  );

  const sendProcessInputTool = tool(
    async ({ processId, data }: { processId: string; data: string }) => {
      try {
        const result = await client.sendProcessInput(sessionId, processId, data);
        return `Wrote ${result.bytes_written} bytes to process ${processId}`;
      } catch (err) {
        if (err instanceof BoxkiteApiError) return `Error sending input to process: ${err.message}`;
        throw err;
      }
    },
    {
      name: "send_process_input",
      description: "Write to a background process's stdin (e.g. answering an interactive prompt in a REPL).",
      schema: z.object({ processId: z.string(), data: z.string() }),
    },
  );

  const stopProcessTool = tool(
    async ({ processId }: { processId: string }) => {
      try {
        const result = await client.stopProcess(sessionId, processId);
        return `Process ${processId}: ${result.status} (exit_code=${result.exit_code})`;
      } catch (err) {
        if (err instanceof BoxkiteApiError) return `Error stopping process: ${err.message}`;
        throw err;
      }
    },
    {
      name: "stop_process",
      description: "Stop a background process (SIGTERM, then SIGKILL if it doesn't exit within a few seconds).",
      schema: z.object({ processId: z.string() }),
    },
  );

  const listProcessesTool = tool(
    async () => {
      try {
        const result = await client.listProcesses(sessionId);
        const processes = (result.processes as any[] | undefined) ?? [];
        if (processes.length === 0) return "(no background processes)";
        return processes
          .map((p) => {
            const desc = p.description ? ` (${p.description})` : "";
            const exit = p.exit_code !== null && p.exit_code !== undefined ? `, exit_code=${p.exit_code}` : "";
            return `${p.process_id}${desc}: ${p.status}${exit} -- ${p.command}`;
          })
          .join("\n");
      } catch (err) {
        if (err instanceof BoxkiteApiError) return `Error listing processes: ${err.message}`;
        throw err;
      }
    },
    {
      name: "list_processes",
      description: "List every background process currently tracked in this sandbox session.",
      schema: z.object({}),
    },
  );

  // Secrets-broker HTTP request tool (docs/SECRETS-DESIGN.md). Opt-in, same
  // as boxkite.tools.factory.create_sandbox_tools's enable_http_request_tool
  // -- this is a new outbound-request surface gated by the session's own
  // secret_names grants and the sidecar's DNS-rebinding-safe destination
  // check, not something every caller wants by default.
  const httpRequestTool = tool(
    async ({
      method,
      url,
      headers,
      body,
    }: {
      method: string;
      url: string;
      headers?: Record<string, string>;
      body?: string;
    }) => {
      try {
        const result = await client.httpRequest(sessionId, method, url, { headers, body });
        const truncatedNote = result.truncated ? " (truncated)" : "";
        return `Status: ${result.status_code}\nHeaders: ${JSON.stringify(result.headers)}\nBody${truncatedNote}:\n${result.body}`;
      } catch (err) {
        if (err instanceof BoxkiteApiError) return `Error making HTTP request: ${err.message}`;
        throw err;
      }
    },
    {
      name: "http_request",
      description:
        "Make an HTTP request to a third-party API, optionally using a granted secret's real credential value via a {{secret:name}} reference in headers/body -- the actual credential is never visible to you; it is substituted server-side by the sidecar immediately before the request is sent.",
      schema: z.object({
        method: z.string(),
        url: z.string(),
        headers: z.record(z.string(), z.string()).optional(),
        body: z.string().optional(),
      }),
    },
  );

  return [
    bashTool,
    fileCreate,
    view,
    strReplace,
    lsTool,
    globTool,
    grepTool,
    startProcessTool,
    getProcessOutputTool,
    sendProcessInputTool,
    stopProcessTool,
    listProcessesTool,
    ...(options?.enableHttpRequestTool ? [httpRequestTool] : []),
  ];
}
