# LLM Pieroot: OmniRoute integration design

## Decision and scope

Use OmniRoute 3.8.52, pinned to upstream commit `edab7c3a59bf7d6f040d581dbda3a46adbee0d57`, vendored under `omniroute/`. Deploy an independent persistent instance at `https://llm.pieroot.xyz`. Following the operator's retirement decision, stop the old Codex LB only after blocking new old-domain requests and observing no accepted work or restart-blocking native bridge work. Preserve its database, credentials, keys, configuration, image and volume for recovery; never silently redirect its clients or copy its OAuth refresh tokens.

## Visual contract (before implementation)

Inherit OmniRoute's existing dashboard shell, navigation, fonts, spacing, light/dark themes, cards, tables, filters, forms, loading and error patterns. No new visual system or parallel dashboard. Extend existing Providers, Costs, Logs, API Manager and Quota pages where a gap is proven. Keep connection identity email-first when an observed email exists, registration name otherwise; never fabricate an email.

- Providers: subscription/API connections, observed identity, auth health, enabled state, routing policy, refresh, actual provider quota and reset. Keep quota observation separate from local budget and rate limits.
- Logs: provider, model, connection/account and API-key filtering; selected connection must remain attributable for Claude and Codex. Never present an identified source as unassigned.
- Costs: reuse Cost Explorer account/provider/model/API-key grouping. Show API-equivalent token-price estimates as estimates, not subscription charges or prepaid balance. Missing price and missing usage remain unknown, not zero/free; show partial known sums and unknown counts.
- API Manager: per-key access and limits, actual input/output/cache tokens and requests, API-equivalent cost, and key-scoped provider/model quota. Configured permission is not current route eligibility. A rate allowance is not subscription capacity.
- OMP: use `omniroute` for the active gateway, with native protocol per model. Preserve legacy `codex-lb` client settings without silently repointing them to the new server. Reuse the existing plugin and status line; no duplicate widget. Display only the selected gateway/model's provider pool, freshness and reporting/unknown counts. No mixed Codex/Claude or cross-gateway percentage.

## API contracts

Gateway authentication uses a gateway API key, not upstream credentials. Dashboard APIs require the upstream dashboard authentication boundary. Never publish subscription tokens or other users' key/account metadata. Preserve API-key model/provider/account constraints in discovery, routing, usage and quota.

Provide `/v1/omp/models` and `/v1/usage/quota?model=<id>` compatibility for the existing OMP plugin, using its existing validated manifest/schema and fixed native roots. A gateway URL cannot cause the plugin to send credentials to an arbitrary manifest URL. Discovery must not invent entitlements or output limits. Quota must come from real provider observations, never the upstream queue-pressure synthetic quota endpoint. Unknown/stale observations stay unknown. Aggregate only authorized connections for the exact model/provider and retain model-specific quota windows.

The `omniroute` OMP provider uses `OMNIROUTE_BASE_URL`, `OMNIROUTE_API_KEY` (or the variable named by `OMNIROUTE_KEY_ENV`), and optional profile settings `extensions/omniroute-v1.json`. Existing `CODEX_LB_*` settings, `codex-lb-v1.json`, model roles, static models and budgets remain unchanged. Namespace connect/model/quota commands and quota UI state per gateway. Both key references must fail closed when absent, including after registering both providers; credentials and quota responses must never cross gateways. The plugin does not rotate credentials or change a session's selected model automatically.

Active authorized Antigravity OAuth connections contribute their synced, visible chat models, including Gemini, Gemini 4, Claude Opus/Sonnet and GPT-OSS, with qualified `antigravity/<model>` IDs and OpenAI Chat Completions at `/v1`. Discovery and inference use the connection's same official IDE/CLI client profile; Gemini 4 is advertised by the CLI profile for the verified account. Reuse canonical catalog capabilities and limits; omit models without known context/output limits and exclude media-only surfaces. OmniRoute plugin discovery uses its manifest alone, not the legacy native Codex union. OMP's provider-scoped cache and explicit YAML static overlays remain host behavior, not proof of current authorization. Antigravity quota is unsupported by the existing Codex/Claude window contract and must stay unavailable rather than inherit another provider's observations, including for Antigravity-hosted Claude models.

Native Claude models advertised by an authorized connected account remain discoverable when their verified canonical capabilities exist but the static provider registry lags. This includes `claude-haiku-5-5`; unknown context/output limits still exclude a model. Existing registered native model metadata and key model/connection restrictions remain unchanged.

## Routing and completion safety

Subscription-first is scoped to same provider family and exact upstream model. Paid API connections are last resort; subscription-only forbids them. Enforce key restrictions before dispatch. Do not change provider/model simply to make a request succeed. Preserve native Codex Responses and Claude Messages payload/tool semantics. After accepted or ambiguous dispatch, do not replay or change continuation ownership. Exhaustion and re-entry state must actually be wired; no helper-only hysteresis claim.

Subscription usage credits are distinct from an API-key balance, API-equivalent cost estimate and quota-reset coupons. Reuse each native account's existing explicit opt-in (`allowPaidCredits=true` for Codex; `blockExtraUsage=false` for Claude), default off/blocked, without changing existing operator consent. For fresh unbound requests, eligible included subscription allowance takes precedence over credit-only accounts. Admit credit fallback only after applicable, fresh subscription observations actually report exhaustion and the provider reports usable credits or extra-usage allowance; missing, stale, expired-reset, zero or over-limit evidence must not authorize spend. Configured quota cutoffs are not proof that the subscription is fully consumed. Preserve model-specific windows, account/connection/key restrictions, cooldowns, leases and continuation ownership; strict subscription-only keys still forbid credit overage.

The dashboard must describe the permission as “use credits automatically after subscription exhaustion,” separately from purchasing credits or enabling automatic top-up. Show Codex values in reported credit units, not dollars; Claude's remaining configured extra-usage spending allowance is not a prepaid wallet balance. Preserve reported currency/decimal scale and distinguish unknown from zero. This gateway does not change provider-side payment, top-up, sharing, or spending settings and cannot promise an upstream hard monetary cap against concurrent/in-flight usage. Verification must not enable credit consent on real accounts or consume real credits without separate authorization.

Codex account rows show observed rolling credit use against saved daily/weekly limits as progress bars, not against unsaved form drafts. Missing consumption and absent limits have no invented percentage; over-limit observations retain their true amount/percentage while only the bar fill is clamped. Show reset coupons separately on the default provider page: read-only provider metadata, grant/expiry dates, locally updated countdown, and unknown/expired states. Do not redeem or create purchases on page load; preserve existing auxiliary-activity lease fences.

## Measurements and controls

Quota capacity estimates use confirmed recorded token deltas and compatible real quota observations. Keep reset, identity and model scopes separate. Never impute missing usage as zero, prorate boundary calls, or call an empirical estimate an official limit. Incomplete usage can provide a labelled lower bound with unknown upper bound. Estimates are display-only, never routing or billing inputs.

Refresh fetches provider metadata without inference. Warm-up is a separately labelled, quota-consuming inference action, gated by eligibility, idempotency/in-flight ownership, reset window and unknown-completion safety. Disabled routes and exhausted subscriptions must not be warmed. Prefer extending existing auto-ping controls rather than adding a second scheduler.

## Delivery and migration

Use separate volume, environment, encryption secrets and loopback port for the new instance. Reuse the existing HAProxy TLS termination with an isolated host route for llm.pieroot.xyz. Back up proxy configuration before a validated reload; do not interrupt accepted Codex LB requests. No direct foreign SQLite or encrypted-blob import: migrate using validated semantic mappings or supported provider credentials import, without exposing credentials. Preserve historical data in the old service until an explicitly verified migration covers it.

## Redistributable extension contract

Distribute the full integration as `omniroute-subscriptions`, a build-time source extension for the exact pinned OmniRoute revision above. The inherited isolated request-hook plugin API cannot register Next routes, dashboard components, database modules or the internal terminal/no-replay guards. Do not describe a source overlay as a dashboard hot-install plugin. Keep canonical implementation under `omniroute/`; generate a public-only, checksum-bound payload and original files from the verified upstream Git tree and archive. Installation is offline, refuses incompatible or locally edited owned files, and never changes credentials, data, dependencies, running services or proxy configuration. Removal is source-only and requires a rebuild; it is not a database rollback.

Reuse the existing public `omp-codex-lb` OMP plugin rather than maintaining a second inference transport. `/omniroute-connect <HTTPS-origin>` saves only profile connection metadata; gateway keys remain externally supplied. Never silently repoint old sessions, rewrite model roles or replay an interrupted turn. A restarted client must explicitly select an authorized `omniroute/<model>` when a resumed session still selects the retired provider.

Account-routing explanations describe the existing runtime, not a new algorithm. Show localized labels with stable strategy IDs, the effective inherited global strategy and sticky count, and the selection rule, appropriate use and limitations. Korean routing UI must retain the exact-model, default-off paid fallback, subscription-only exclusion, no-replay and current-password protections.

## Acceptance and verification

1. Unauthenticated inference/discovery/quota and private usage fail closed; authenticated keys see only their allowed scope.
2. Codex and Claude native inference and tool calls complete through the new domain with attributable key/provider/connection usage.
3. Same-provider/exact-model subscription-first fallback, subscription-only exclusion, and ambiguous completion refusal are exercised.
4. Existing OMP plugin discovers permitted models and displays provider/model-specific real quota without rewriting other providers or roles.
5. Dashboard shows per-key requests/tokens/API-equivalent costs and distinguishes unknown values, estimates, and actual balances.
6. Existing refresh/warm-up controls and empirical capacity semantics are verified; no fabricated provider quota.
7. Container builds and actual supported-platform execution pass, public TLS/auth works, the retired service's recoverable data remains intact, and installation/operation instructions describe only exercised behavior.

## Development ownership

Use Luna/Haiku subagents for bounded implementation and independent review. Integration owner retains security, deployment and end-to-end verification. Each slice owns separate files; no mid-flight full build/lint/format/test runs. Run integrated checks once after edits converge. Record blockers as blockers rather than deploying a misleading partial replacement.
