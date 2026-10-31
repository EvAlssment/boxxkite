/**
 * Runnable end-to-end example against a real hosted control-plane.
 * Build first, then run `node dist/examples/snapshots.js` with
 * BOXXKITE_BASE_URL and BOXXKITE_API_KEY set.
 */

import { BoxxkiteApiError, BoxxkiteClient } from "../src/index.js";

async function main() {
  const baseUrl = process.env.BOXXKITE_BASE_URL;
  const apiKey = process.env.BOXXKITE_API_KEY;
  if (!baseUrl || !apiKey) {
    console.error("Set BOXXKITE_BASE_URL and BOXXKITE_API_KEY first.");
    process.exit(1);
  }

  const client = new BoxxkiteClient({ baseUrl, apiKey });
  const source = await client.createSandbox({ label: "snapshot-example-source" });
  let snapshotId: string | undefined;
  let restoredId: string | undefined;
  try {
    const snapshot = await client.createSnapshot(source.id, { label: "checkpoint" });
    snapshotId = snapshot.id;
    console.log(`Created snapshot ${snapshot.id} (${snapshot.status})`);
    console.log(`Snapshots for source: ${(await client.listSnapshots(source.id)).length}`);

    const details = await client.getSnapshot(snapshot.id);
    if (details.status !== "completed") {
      throw new Error(`Snapshot is not ready to restore: ${details.status}`);
    }

    const restored = await client.restoreSnapshot(snapshot.id, { label: "snapshot-example-restored" });
    restoredId = restored.id;
    console.log(`Restored snapshot into sandbox ${restored.id}`);
  } catch (err) {
    if (err instanceof BoxxkiteApiError) {
      console.error(`API error: ${err.message} [${err.code}]`);
      process.exitCode = 1;
    } else {
      throw err;
    }
  } finally {
    if (restoredId) await client.destroySandbox(restoredId);
    if (snapshotId) await client.deleteSnapshot(snapshotId);
    await client.destroySandbox(source.id);
  }
}

main();
