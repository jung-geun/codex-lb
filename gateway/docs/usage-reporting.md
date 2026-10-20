# Usage Reporting

codex-lb records the token counts reported in the terminal Responses API event. It does not retokenize prompts or responses, and it does not estimate hidden reasoning usage.

For direct Codex traffic over HTTP or WebSocket, the reported buckets have these relationships:

- `input_tokens` includes the full input count; cached input is reported as a subset.
- `output_tokens` includes all generated output, including reasoning tokens.
- `reasoning_tokens` is the reported reasoning subset of `output_tokens`.
- Total tokens are `input_tokens + output_tokens`. Do not add cached input or reasoning tokens again.

## Dashboard

The **Request Logs** token cell shows total tokens, with reported cached-input and reasoning counts underneath. Open **Details** to see the exact reported reasoning count and its relationship to output tokens.

The **Reports** page shows the reported reasoning total for the selected date range and filters. Its coverage count states how many requests supplied a reasoning value. The daily breakdown and CSV export include the same reasoning field.

## Missing Usage

A reported zero remains `0`. A missing value remains unknown and appears as `—` in the daily report or is omitted from request details. codex-lb does not turn missing usage into zero.

Reasoning usage may be missing when the upstream terminal event does not include it, when a stream ends before that event arrives, or for older request-log rows. The dashboard does not backfill those rows. Custom OpenAI-compatible model sources do not currently feed reasoning details into this reporting path.

## Subscription source monitoring

Accounts presents observed Claude plan/account/organization metadata, credential presence and expiry, and remaining quota for each provider window and model. Unknown capacity is not zero or unlimited; stale observations, provider errors, exhaustion and reauthorization remain distinct. Dashboard lists Claude subscriptions separately from Codex credit accounts and never adds their percentage quotas to credit balances.

Quota history stores successful provider observations for thirty days. The default seven-day read returns the latest actual sample per hour/window/model with its original timestamp; the chart does not reconstruct missing days or assume a reset replenished capacity. Migration seeds only the existing latest observations. Credential replacement and a verified principal change prevent another principal's history from being reused.

Reports supports account/source and provider-type filters, plus account/source and type breakdowns. Source identity/type survive raw-log retention and source deletion in permanent folds. Migration preserves additive totals and the fold watermark; irrecoverable legacy attribution remains unknown. Known API-equivalent cost is a subtotal, not a subscription charge, and unpriced requests are disclosed rather than treated as free.

Source account, quota-history and observed-usage snapshots are permission-protected and use `Cache-Control: no-store`. Unauthorized client caches are hidden until fresh authorization succeeds. Global visual privacy also masks account/organization identities embedded in quota-group labels.

## Pricing and Missing Costs

codex-lb refreshes OpenAI text-token pricing hourly from models.dev. Compatible LiteLLM data supplies missing tier rates and additional models. Price lookups use the in-memory catalog; an upstream outage falls back to the last successful disk cache and bundled pricing snapshot. These amounts use API-equivalent token prices, not ChatGPT subscription credit multipliers.

When a model's price becomes available, retained subscription requests with missing costs are repaired automatically in small batches. Their dashboard, report, account, and API-key usage aggregates are corrected in the same transaction. Existing costs, including zero, and API-key limit counters are preserved. Requests whose raw logs have already been deleted by retention cannot be reconstructed.

The Codex client-version fallback is also retained across restarts. Bundled prices and the stable Codex version are maintained by a daily update workflow that opens a reviewable PR. Operators do not need an extra API key or configuration setting.

*Spec: [upstream-metadata](https://github.com/Soju06/codex-lb/tree/main/openspec/specs/upstream-metadata)*

---

*Spec: [frontend-architecture](https://github.com/Soju06/codex-lb/tree/main/openspec/specs/frontend-architecture)*
