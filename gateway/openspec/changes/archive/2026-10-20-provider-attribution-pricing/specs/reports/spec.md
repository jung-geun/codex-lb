## ADDED Requirements

### Requirement: Request logs use provider-aware account selectors and presentation

Request-log listing, options and dashboard account controls MUST include native accounts and model-source identities in one existing account selector. Native selectors MUST retain plain account IDs and model-source selectors MUST use `source:<sourceId>`. Selected identities within one dimension SHALL be ORed and intersected with other active filters. Source attribution MUST NOT be written into the native-account foreign key. Display labels SHALL prefer an observed source account email, otherwise a registration label, otherwise a retained opaque identity; known source traffic MUST NOT be displayed as unassigned solely because its native account ID is null. Deleted-source history and user-scoped authorization MUST remain intact.

#### Scenario: Claude source appears beside Codex accounts
- **GIVEN** a Codex account and a Claude source both have retained request logs
- **WHEN** request logs and matching account options are loaded
- **THEN** both identities are selectable and selecting the source includes only its matching traffic
- **AND** a same-ID native account remains a separate identity

### Requirement: Provider type distribution uses existing report metrics

The Reports dashboard MUST expose a provider/account-type distribution using recorded `byType` aggregates, the existing cost/request metric switch and visual conventions. It MUST respect every active report filter and preserve exact provider names including unknown types. Known-cost subtotals and unknown-price counts MUST remain distinguishable; an unknown-only provider MUST NOT be silently represented as free or omitted from request-count distribution.

#### Scenario: Provider has only unpriced requests
- **GIVEN** one provider has known costs and another has only unpriced requests
- **WHEN** the request-count metric is selected
- **THEN** both appear with their exact request totals
- **AND** cost presentation discloses the unpriced provider rather than claiming zero cost

### Requirement: Missing API-equivalent costs are repaired only from supported evidence

Missing request costs SHALL be computed only when exact provider/model rate evidence and the required recorded usage components support a complete calculation. Cache reads, cache writes and applicable service/context tiers MUST be accounted for without double counting. Existing known costs MUST NOT be overwritten. Retained raw requests and already-folded account/key/time/report aggregates MUST receive consistent bounded, idempotent deltas under existing fold synchronization; unrelated requests, unknown counts and pruned history MUST NOT be invented or rebuilt. An exact model without verified price evidence or a request without required usage MUST remain unknown and its limitation SHALL be disclosed.

#### Scenario: Repair after partial raw retention
- **GIVEN** a retained unpriced request has supported usage and verified rates, and its bucket also contains pruned history
- **WHEN** bounded cost repair runs twice
- **THEN** the first run adds its supported cost once and updates matching unknown-price counts
- **AND** the second run changes nothing and all unrelated permanent history remains unchanged

#### Scenario: Inconsistent history does not block supported later repairs
- **GIVEN** an unknown-cost row has no matching historical report bucket
- **WHEN** repair scans it alongside later supported rows
- **THEN** all deltas for the inconsistent row are rolled back and its cost stays unknown
- **AND** a diagnostic is emitted while the scan advances and later supported rows can be repaired

