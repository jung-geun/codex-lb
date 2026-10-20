# Provider attribution and API-equivalent pricing

## Why

Request-log account presentation and selectors currently omit model-source accounts even though source attribution is retained. Reports expose provider totals as a table but not the same distribution visualization offered for models and User-Agents. Missing costs need an evidence-based inventory, provider-specific published prices, and bounded repair without overwriting known costs or rebuilding pruned history.

## What changes

- Extend the existing request-log account selector and display to include source-backed accounts, with canonical provider metadata and observed email labels. Preserve native account foreign keys and distinguish namespaced source selectors.
- Add provider/type distribution to the existing Reports presentation using current chart, metric toggle, and unknown-price conventions.
- Resolve complete, officially supported price/usage combinations for future requests and retained history. Preserve unavailable usage and unpublished exact-model rates as unknown; never substitute a sibling model's price or describe subscription estimates as billed subscription cost.
- Explain the existing API-key source-model/effective-access panel without silently changing access policy.

## Non-goals

No inference replay, continuation-ownership bypass, OAuth rotation, paid fallback trial, credential changes, or unreviewed upstream merge. The separately investigated connection incident remains read-only.

## Ownership and verification

Request attribution, pricing/backfill, and report visualization are independent implementation slices. Schema/query/serialization changes must preserve user-scoped authorization, date/status/key/model filtering, stable pagination, deleted-source history, and unknown-vs-zero semantics. Verify targeted behavior, actual isolated UI, and application images before publishing.
