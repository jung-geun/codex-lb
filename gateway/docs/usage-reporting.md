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

The Request Logs **Accounts** filter includes both native Codex accounts and other model-source accounts. The existing repeated `accountId` parameter accepts native IDs unchanged and `source:<id>` for sources; selections are ORed within this filter and intersected with date, API-key, model and status filters. Source rows prefer an observed account email, then their registration name, then an opaque retained selector. A null native-account ID no longer makes known source traffic appear unassigned. Identity metadata and identity search retain the dashboard's existing Accounts-write permission gate; deleted-source history stays selectable without exposing removed credentials.

Reports uses the same existing donut design for **Distribution by Provider**. COST displays known subtotals and unknown-cost coverage; REQ includes every provider's matching requests even when its costs are unknown. The selected report filters apply to both metrics.

## Missing Usage

A reported zero remains `0`. A missing value remains unknown and appears as `—` in the daily report or is omitted from request details. codex-lb does not turn missing usage into zero.

Reasoning usage may be missing when the upstream terminal event does not include it, when a stream ends before that event arrives, or for older request-log rows. The dashboard does not backfill those rows. Custom OpenAI-compatible model sources do not currently feed reasoning details into this reporting path.

## Subscription source monitoring

Accounts presents observed Claude plan/account/organization metadata, credential presence and expiry, and remaining quota for each provider window and model using the existing account components. Unknown capacity is not zero or unlimited; stale observations, provider errors, exhaustion and reauthorization remain distinct. Dashboard includes these sources in the existing account grid/list but never adds their percentage quotas to Codex credit balances.

Quota history stores successful provider observations for thirty days. The default seven-day read returns the latest actual sample per hour/window/model with its original timestamp; the chart does not reconstruct missing days or assume a reset replenished capacity. Migration seeds only the existing latest observations. Credential replacement and a verified principal change prevent another principal's history from being reused.

Reports supports account/source and provider-type filters, plus account/source and type breakdowns. Source identity/type survive raw-log retention and source deletion in permanent folds. Migration preserves additive totals and the fold watermark; irrecoverable legacy attribution remains unknown. Known API-equivalent cost is a subtotal, not a subscription charge, and unpriced requests are disclosed rather than treated as free.

Source account, quota-history and observed-usage snapshots are permission-protected and use `Cache-Control: no-store`. Unauthorized client caches are hidden until fresh authorization succeeds. Global visual privacy also masks account/organization identities embedded in quota-group labels.

## Pricing and Missing Costs

codex-lb refreshes OpenAI text-token pricing hourly from models.dev. Compatible LiteLLM data supplies missing tier rates and additional models. Price lookups use the in-memory catalog; an upstream outage falls back to the last successful disk cache and bundled pricing snapshot. These amounts use API-equivalent token prices, not ChatGPT subscription credit multipliers.

Retained native and model-source requests with missing costs are repaired automatically in bounded batches when exact rates and complete required usage are available. Their matching dashboard, report, account and API-key usage aggregates receive only the supported deltas under the existing fold lock and transaction. Existing costs, including zero, API-key limit counters and pruned permanent history are preserved. A missing or inconsistent folded report bucket rolls back all deltas for that row, leaves its cost unknown and logs its numeric row ID; later rows continue. Unexpected transaction failures roll back the batch. A second pass does not add supported cost again.

The Codex client-version fallback is also retained across restarts. Bundled prices and the stable Codex version are maintained by a daily update workflow that opens a reviewable PR. Operators do not need an extra API key or configuration setting.

### Verified Claude token rates

Exact first-party Claude model prices are shipped from [Anthropic's pricing documentation](https://platform.claude.com/docs/en/about-claude/pricing), verified on 2026-10-21. USD per million tokens:

| Exact model ID | Input | Cache read | 5m cache write | 1h cache write | Output |
| --- | ---: | ---: | ---: | ---: | ---: |
| `claude-haiku-5-5` | 0.10 | 0.01 | 0.125 | 0.20 | 0.50 |
| `claude-sonnet-5-5` | 2 | 0.10 | 2.50 | 4 | 10 |
| `claude-opus-5-5` | 4 | 0.20 | 5 | 8 | 20 |
| `claude-fable-5-1` | 10 | 0.25 | 12.50 | 20 | 50 |
| `claude-opus-5` | 5 | 0.50 | 6.25 | 10 | 25 |
| `claude-sonnet-5` | 2 | 0.20 | 2.50 | 4 | 10 |
| `claude-fable-5` | 10 | 1 | 12.50 | 20 | 50 |
| `claude-opus-4-8` | 5 | 0.50 | 6.25 | 10 | 25 |
| `claude-opus-4-7` | 5 | 0.50 | 6.25 | 10 | 25 |
| `claude-sonnet-4-6` | 3 | 0.30 | 3.75 | 6 | 15 |
| `claude-opus-4-6` | 5 | 0.50 | 6.25 | 10 | 25 |
| `claude-opus-4-5-20251101` | 5 | 0.50 | 6.25 | 10 | 25 |
| `claude-haiku-4-5-20251001` | 1 | 0.10 | 1.25 | 2 | 5 |
| `claude-sonnet-4-5-20250929` | 3 | 0.30 | 3.75 | 6 | 15 |

Explicit configured component rates take precedence, including zero. Otherwise defaults require an exact provider and served-model match; historical repair uses recorded identities rather than guessing a current alias. Haiku 5.5 prompts strictly above 100,000 total input tokens (including cache reads/writes) use 5× rates. Published Opus 5.5, Opus 5 and Opus 4.8 fast mode uses 2× when explicitly recorded; OpenAI priority/flex tiers are not assumed to mean Claude fast mode. Incomplete cache-write TTL splits keep cost unknown.

These are standard first-party API-equivalent estimates, not reconstructed invoices. Missing historical speed, batch, residency and server-tool billing metadata cannot establish those modifiers. Token usage not reported by a failed or interrupted request cannot be recovered by adding a rate. `codex-auto-review` remains unknown without an exact published token rate or authoritative served-model mapping; free subscription access does not establish a zero API-equivalent price.

OpenAI exact fallback rates follow [official pricing](https://developers.openai.com/api/docs/pricing), including GPT-6.1 Sol context/tier prices and the current GPT-5.6 Sol promotional rates. Known historical amounts are not overwritten merely because a current rate changed.

*Spec: [reports](../openspec/specs/reports/spec.md)*

*Spec: [upstream-metadata](https://github.com/Soju06/codex-lb/tree/main/openspec/specs/upstream-metadata)*

---

*Spec: [frontend-architecture](https://github.com/Soju06/codex-lb/tree/main/openspec/specs/frontend-architecture)*
