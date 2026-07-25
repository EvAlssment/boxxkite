/**
 * Vercel AI SDK (`ai` package, v5's `tool()` shape) tool factory over a
 * hosted boxkite control-plane.
 *
 * Same tool set and behavior as ./langchain.ts (bash_tool, file_create,
 * view, str_replace, ls, glob, grep, the five process tools, and the
 * opt-in http_request tool) -- this file exists because the Vercel AI SDK's
 * `tool()` helper has its own shape (`inputSchema` + `execute`, not
 * LangChain's `tool(fn, config)`), not because the underlying sandbox
 * operations differ. Requires the `ai` package (v5+) as a peer dependency;
 * import from "boxkite-client/vercel-ai", not the package root, so
 * consumers who don't use the Vercel AI SDK never need to install it.
 *
 * Usage with `generateText`/`streamText`:
 *
 *   import { generateText } from "ai";
 *   import { openai } from "@ai-sdk/openai";
 *   import { createSandboxTools } from "boxkite-client/vercel-ai";
 *
 *   const tools = createSandboxTools(client, sessionId);
 *   const result = await generateText({ model: openai("gpt-5"), tools, prompt, maxSteps: 5 });
 */

import { tool } from "ai";
import { z } from "zod";

import { BoxkiteClient } from "./client.js";
import { BoxkiteApiError } from "./errors.js";

export function createSandboxTools(
  client: BoxkiteClient,
  sessionId: string,
  options?: { enableHttpRequestTool?: boolean },
) {
  const bash_tool = tool({
    description:
      "Run a shell command in the sandbox. Returns stdout, or stderr and the exit code if the command failed.",
    inputSchema: z.object({ command: z.string() }),
    execute: async ({ command }) => {
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
  });

  const file_create = tool({
    description: "Create or overwrite a file in the sandbox workspace.",
    inputSchema: z.object({ path: z.string(), content: z.string() }),
    execute: async ({ path, content }) => {
      try {
        const result = await client.fileCreate(sessionId, path, content);
        return `Wrote ${result.path ?? path} (${result.size ?? content.length} bytes)`;
      } catch (err) {
        if (err instanceof BoxkiteApiError) return `Error creating file: ${err.message}`;
        throw err;
      }
    },
  });

  const view = tool({
    description: "View a file's contents, or list a directory's entries.",
    inputSchema: z.object({ path: z.string() }),
    execute: async ({ path }) => {
      try {
        const result = await client.view(sessionId, path);
        return (result.content as string | undefined) ?? JSON.stringify(result);
      } catch (err) {
        if (err instanceof BoxkiteApiError) return `Error viewing ${path}: ${err.message}`;
        throw err;
      }
    },
  });

  const str_replace = tool({
    description:
      "Replace a unique string in a sandbox file. old_str must appear exactly once in the file.",
    inputSchema: z.object({ path: z.string(), old_str: z.string(), new_str: z.string() }),
    execute: async ({ path, old_str, new_str }) => {
      try {
        const result = await client.strReplace(sessionId, path, old_str, new_str);
        return `Replaced in ${result.path ?? path} (${result.occurrences ?? 1} replacement(s))`;
      } catch (err) {
        if (err instanceof BoxkiteApiError) return `Error editing ${path}: ${err.message}`;
        throw err;
      }
    },
  });

  const ls = tool({
    description: "List the direct children of a directory in the sandbox workspace.",
    inputSchema: z.object({ path: z.string().optional() }),
    execute: async ({ path }) => {
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
  });

  const glob = tool({
    description:
      "Find files by name pattern (e.g. '**/*.py') under a directory in the sandbox workspace.",
    inputSchema: z.object({ pattern: z.string(), path: z.string().optional() }),
    execute: async ({ pattern, path }) => {
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
  });

  const grep = tool({
    description:
      "Search file contents by regex pattern under a directory in the sandbox workspace, optionally restricted to files matching a glob.",
    inputSchema: z.object({
      pattern: z.string(),
      path: z.string().optional(),
      glob: z.string().optional(),
      maxMatches: z.number().optional(),
    }),
    execute: async ({ pattern, path, glob: globPattern, maxMatches }) => {
      try {
        const result = await client.grep(sessionId, pattern, {
          path,
          glob: globPattern,
          maxMatches,
        });
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
  });

  const start_process = tool({
    description:
      "Start a long-running background process in the sandbox (a dev server, a test watcher, a long build, a REPL) that keeps running after this tool call returns. Distinct from bash_tool, which is one-shot and bounded by its own timeout.",
    inputSchema: z.object({
      command: z.string(),
      description: z.string().optional(),
      maxRuntimeSeconds: z.number().optional(),
    }),
    execute: async ({ command, description, maxRuntimeSeconds }) => {
      try {
        const result = await client.startProcess(sessionId, command, {
          description,
          maxRuntimeSeconds,
        });
        return `Started process ${result.process_id} (status=${result.status}). Use get_process_output("${result.process_id}") to check on it.`;
      } catch (err) {
        if (err instanceof BoxkiteApiError) return `Error starting process: ${err.message}`;
        throw err;
      }
    },
  });

  const get_process_output = tool({
    description:
      "Poll a background process's output since a given byte offset. Call repeatedly to watch a process's progress.",
    inputSchema: z.object({ processId: z.string(), sinceOffset: z.number().optional() }),
    execute: async ({ processId, sinceOffset }) => {
      try {
        const result = await client.getProcessOutput(sessionId, processId, { sinceOffset });
        const lines = [`status: ${result.status}`];
        if (result.exit_code !== null && result.exit_code !== undefined)
          lines.push(`exit_code: ${result.exit_code}`);
        if (result.truncated)
          lines.push("(earlier output was truncated -- the buffer only keeps recent output)");
        lines.push(`next_offset: ${result.next_offset}`);
        lines.push("--- output ---");
        lines.push(result.stdout_chunk || "(no new output)");
        return lines.join("\n");
      } catch (err) {
        if (err instanceof BoxkiteApiError) return `Error getting process output: ${err.message}`;
        throw err;
      }
    },
  });

  const send_process_input = tool({
    description: "Write to a background process's stdin (e.g. answering an interactive prompt in a REPL).",
    inputSchema: z.object({ processId: z.string(), data: z.string() }),
    execute: async ({ processId, data }) => {
      try {
        const result = await client.sendProcessInput(sessionId, processId, data);
        return `Wrote ${result.bytes_written} bytes to process ${processId}`;
      } catch (err) {
        if (err instanceof BoxkiteApiError) return `Error sending input to process: ${err.message}`;
        throw err;
      }
    },
  });

  const stop_process = tool({
    description: "Stop a background process (SIGTERM, then SIGKILL if it doesn't exit within a few seconds).",
    inputSchema: z.object({ processId: z.string() }),
    execute: async ({ processId }) => {
      try {
        const result = await client.stopProcess(sessionId, processId);
        return `Process ${processId}: ${result.status} (exit_code=${result.exit_code})`;
      } catch (err) {
        if (err instanceof BoxkiteApiError) return `Error stopping process: ${err.message}`;
        throw err;
      }
    },
  });

  const list_processes = tool({
    description: "List every background process currently tracked in this sandbox session.",
    inputSchema: z.object({}),
    execute: async () => {
      try {
        const result = await client.listProcesses(sessionId);
        const processes = (result.processes as any[] | undefined) ?? [];
        if (processes.length === 0) return "(no background processes)";
        return processes
          .map((p) => {
            const desc = p.description ? ` (${p.description})` : "";
            const exit =
              p.exit_code !== null && p.exit_code !== undefined ? `, exit_code=${p.exit_code}` : "";
            return `${p.process_id}${desc}: ${p.status}${exit} -- ${p.command}`;
          })
          .join("\n");
      } catch (err) {
        if (err instanceof BoxkiteApiError) return `Error listing processes: ${err.message}`;
        throw err;
      }
    },
  });

  // Secrets-broker HTTP request tool (docs/SECRETS-DESIGN.md), same opt-in
  // gate as sdk-js/src/langchain.ts's own httpRequestTool.
  const http_request = tool({
    description:
      "Make an HTTP request to a third-party API, optionally using a granted secret's real credential value via a {{secret:name}} reference in headers/body -- the actual credential is never visible to you; it is substituted server-side by the sidecar immediately before the request is sent.",
    inputSchema: z.object({
      method: z.string(),
      url: z.string(),
      headers: z.record(z.string(), z.string()).optional(),
      body: z.string().optional(),
    }),
    execute: async ({ method, url, headers, body }) => {
      try {
        const result = await client.httpRequest(sessionId, method, url, { headers, body });
        const truncatedNote = result.truncated ? " (truncated)" : "";
        return `Status: ${result.status_code}\nHeaders: ${JSON.stringify(result.headers)}\nBody${truncatedNote}:\n${result.body}`;
      } catch (err) {
        if (err instanceof BoxkiteApiError) return `Error making HTTP request: ${err.message}`;
        throw err;
      }
    },
  });

  return {
    bash_tool,
    file_create,
    view,
    str_replace,
    ls,
    glob,
    grep,
    start_process,
    get_process_output,
    send_process_input,
    stop_process,
    list_processes,
    ...(options?.enableHttpRequestTool ? { http_request } : {}),
  };
}
