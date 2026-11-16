# Resolved Ticket #1117 — Webhook Payloads Not Delivering After API Key Rotation

**Product:** Acme
**Category:** Resolved Ticket
**Status:** Resolved
**Date:** 2025-01-08

## Original Question

> Hi, we rotated our Acme API keys yesterday for security reasons. Everything else works, but our webhooks stopped firing. We haven't changed anything in our endpoint code. What could cause this? Is there a separate webhook signing secret?

## Final Answer

Hi James,

Thanks for the detailed report. You're spot on — webhook delivery uses a **separate signing secret**, not your regular API key, but the issue in your case was slightly different.

When you rotated your API keys, the workspace's **webhook targets** kept their existing signatures, but the event pipeline began rejecting outgoing deliveries because our system detected that the associated API key version was no longer valid. This triggers an internal delivery freeze on the affected webhook to protect against unauthorized event forwarding.

Here's what we did to resolve it:

1. Located your two webhook targets under **Settings → Webhooks**:
   - `https://api.oldco.io/acme-events` (production)
   - `https://staging.oldco.io/acme-events` (staging)
2. Re-linked each target to your new API key version.
3. Re-verified the signing secret on file for both endpoints.
4. Replayed the failed deliveries from the last 24 hours to your production endpoint.

**Before you confirm resolution, please check the following on your side:**

- Your endpoint should accept POST requests with a `X-Acme-Signature` header.
- Return a `2xx` status within **5 seconds**; otherwise we treat the delivery as failed.
- After a webhook fails 3 consecutive times, you must manually re-enable it at **Settings → Webhooks**.

Our logs show our replayed test deliveries returned `200 OK` to both endpoints. Let us know if anything looks off on your side.

**Resolution summary:** Rotated API keys had invalidated webhook targets. We re-linked the targets to the new key version, verified signing secrets, and replayed undelivered events.

Best,
Priya, Acme Support