# Provider attribution and API-equivalent pricing

## Decisions

- Extend the existing Request Logs Accounts filter rather than add a second source filter. Reuse Reports' `source:<id>` namespace; never write a source into a native account FK.
- Prefer observed source account labels to registration names. Preserve private identity/search gating and expose only public OAuth identity columns. Display provider metadata so native and Claude accounts sharing one email remain distinguishable.
- Reuse the existing model-distribution donut and metric toggle for Provider distribution, replacing the redundant type table. Request counts include unknown-cost providers; known cost is only a subtotal.
- Keep missing rates and missing usage distinct from a known zero. Requests that fail before final usage cannot be priced just by adding a model rate. Positive costs below one cent display `<$0.01` instead of appearing free.
- Verified exact first-party prices are defaults, not replacements for explicit per-component configured prices. Standard API-equivalent estimates are not subscription invoices or reconstructed batch/residency/server-tool charges.
- Repair retained unknown costs with deltas under existing fold synchronization. Do not rebuild permanent aggregates from a partially retained raw history. Savepoints isolate an inconsistent historical report bucket: leave that row unknown with a diagnostic, continue later rows, and roll the whole batch back only for unexpected failures.
- The API-key source-access panel is a read-only configured model/policy intersection and eligibility snapshot. Viewing it does not grant access, edit credentials or enable paid fallback.

## Example

A Claude Opus 5.5 response reports 1,000 total input tokens, including 500 cache reads and 100 five-minute cache writes, plus 100 output tokens. At the verified standard prices the API-equivalent cost is `(400×4 + 500×0.20 + 100×5 + 100×20)/1,000,000 = $0.0042`. Input/cache components are not double counted. A missing cache-write TTL split remains unknown rather than assuming five minutes.

## Verification evidence

The real compiled dashboard and isolated FastAPI/SQLite runtime exercised source-only filtering, source email/provider display, deleted-source selection, provider cost/request toggles, type-filter intersection and a narrow-screen legend. No production credentials were used in the UI fixture. Exact-price/context/cache/tier and folded-accounting regression scenarios cover known zero, source/native/provider identity collisions, pruned history, unexpected transaction rollback and idempotence.

Official source links and the supported price table are maintained in `docs/usage-reporting.md`. Private runtime logs, production database snapshots, operator paths, incident correlation and deployment evidence are intentionally not part of this public change folder.

## Operational caution

Back up a live WAL database from a pinned read transaction or another supported consistent snapshot. An incremental backup without a pinned snapshot can restart repeatedly under continuous writes. Store the copy where sufficient space is verified; do not assume container storage and the host application's filesystem share capacity. Verify the copied database and encryption key before using the copy for accounting checks.
