/**
 * Runnable end-to-end example against a real hosted control-plane.
 * Build first (relative imports resolve against dist/, not this .ts file):
 *
 *   npm run build
 *   BOXKITE_BASE_URL=https://your-control-plane BOXKITE_API_KEY=bxk_live_... \
 *       node dist/examples/quickstart.js
 */

import { BoxkiteApiError, BoxkiteClient } from "../src/index.js";

async function main() {
  const baseUrl = process.env.BOXKITE_BASE_URL;
  const apiKey = process.env.BOXKITE_API_KEY;
  if (!baseUrl || !apiKey) {
    console.error("Set BOXKITE_BASE_URL and BOXKITE_API_KEY first.");
    process.exit(1);
  }

  const client = new BoxkiteClient({ baseUrl, apiKey });

  const account = await client.account();
  console.log(`Signed in as ${account.email}`);

  const usage = await client.usage();
  console.log(
    `Usage: ${usage.monthly_sandbox_hours_used}/${usage.monthly_sandbox_hours_limit} sandbox-hours, ` +
      `${usage.concurrent_sandboxes}/${usage.concurrent_sandboxes_limit} concurrent`,
  );

  try {
    await client.withSandbox(
      async (sb) => {
        console.log(`Created sandbox ${sb.id}`);

        const result = await sb.exec("python3 -c 'print(1 + 1)'");
        console.log(`exec result: ${result.stdout.trim()}`);

        await sb.fileCreate("hello.txt", "hello from boxkite-client (js)\n");
        const viewed = await sb.view("hello.txt");
        console.log(`file contents: ${viewed.content.trim()}`);
      },
      { label: "sdk-js-quickstart" },
    );
    console.log("Sandbox destroyed.");
  } catch (err) {
    if (err instanceof BoxkiteApiError) {
      console.error(`API error: ${err.message} [${err.code}]`);
      process.exit(1);
    }
    throw err;
  }
}

main();
