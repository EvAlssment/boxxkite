/**
 * Runnable end-to-end example against a real hosted control-plane.
 * Build first (relative imports resolve against dist/, not this .ts file):
 *
 *   npm run build
 *   BOXKITE_BASE_URL=https://your-control-plane BOXKITE_API_KEY=bxk_live_... \
 *       node dist/examples/webhooks.js
 */

import * as crypto from "node:crypto";

import { BoxkiteApiError, BoxkiteClient } from "../src/index.js";

/** Verify an `X-Boxkite-Webhook-Signature` header, per docs/WEBHOOKS-DESIGN.md §6. */
function verifySignature(
  secret: string,
  signatureHeader: string,
  rawBody: Buffer,
  toleranceSeconds = 300,
): boolean {
  const parts = Object.fromEntries(
    signatureHeader.split(",").map((p) => {
      const [key, value] = p.split("=", 2);
      return [key, value];
    }),
  );
  const timestamp = Number(parts.t);
  const signature = parts.v1;
  if (Math.abs(Date.now() / 1000 - timestamp) > toleranceSeconds) {
    return false;
  }
  const signedData = Buffer.concat([Buffer.from(`${timestamp}.`), rawBody]);
  const expected = crypto.createHmac("sha256", secret).update(signedData).digest("hex");

  const expectedBuf = Buffer.from(expected, "hex");
  const signatureBuf = Buffer.from(signature, "hex");
  if (expectedBuf.length !== signatureBuf.length) {
    return false;
  }
  return crypto.timingSafeEqual(expectedBuf, signatureBuf);
}

async function main() {
  const baseUrl = process.env.BOXKITE_BASE_URL;
  const apiKey = process.env.BOXKITE_API_KEY;
  if (!baseUrl || !apiKey) {
    console.error("Set BOXKITE_BASE_URL and BOXKITE_API_KEY first.");
    process.exit(1);
  }

  const client = new BoxkiteClient({ baseUrl, apiKey });

  try {
    const webhook = await client.createWebhook({
      url: "https://example.com/boxkite-webhook",
      eventTypes: ["sandbox.created", "sandbox.destroyed", "audit_log.entry"],
      description: "webhooks example",
    });
    console.log(`Created webhook ${webhook.id}`);
    console.log(`Signing secret (shown once, save it now): ${webhook.secret}`);

    // Simulate a delivery to prove verifySignature works, without a real
    // receiver: sign a synthetic payload locally with the just-printed
    // secret, then verify it the same way a receiver would.
    // createWebhook is the one route that returns the signing secret.
    const secret: string = webhook.secret ?? "";
    const rawBody = Buffer.from(
      JSON.stringify({ event_type: "sandbox.created", event_id: "evt_demo" }),
    );
    const timestamp = Math.floor(Date.now() / 1000);
    const signedData = Buffer.concat([Buffer.from(`${timestamp}.`), rawBody]);
    const signature = crypto.createHmac("sha256", secret).update(signedData).digest("hex");
    const signatureHeader = `t=${timestamp},v1=${signature}`;

    const isValid = verifySignature(secret, signatureHeader, rawBody);
    console.log(`Locally signed payload verifies: ${isValid}`);

    await client.deleteWebhook(webhook.id);
    console.log("Webhook deleted.");
  } catch (err) {
    if (err instanceof BoxkiteApiError) {
      console.error(`API error: ${err.message} [${err.code}]`);
      process.exit(1);
    }
    throw err;
  }
}

main();
