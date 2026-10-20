# account-quota-presentation Specification

## Purpose
Governs how account quota windows are presented across the dashboard when an account does not fit the paid 5h/7d shape. Free accounts report a single monthly window, and rendering it as weekly produced wrong bars, trends, donut totals, and assigned-account badges. This capability keeps monthly-only accounts labelled as such, omits zero-credit accounts from window totals they cannot contribute to, and keeps quota refreshes visually stable.
## Requirements
### Requirement: Free-account quota surfaces are monthly-only

When an account's normalized quota model is monthly-only, account-facing quota surfaces SHALL present only the monthly window and MUST NOT render synthetic 5h or 7d bars for that account.

#### Scenario: Account surfaces show only monthly quota
- **WHEN** an account summary carries a normalized monthly quota window with no normalized 5h or 7d windows
- **THEN** the account card, account list row, and account detail usage panel show a single `Monthly` quota bar
- **AND** those surfaces do not render `5h` or `Weekly` bars for that account

### Requirement: Free-account overview quota hides 5h and 7d semantics

Overview and aggregate quota surfaces SHALL treat normalized monthly-only free-account quota as a 30d window and MUST NOT present that account as a weekly-only or dual-window account.

#### Scenario: Overview uses monthly semantics for free accounts
- **WHEN** overview data includes a free account with only a normalized monthly quota window
- **THEN** the overview account quota display shows only the 30d window for that account
- **AND** the account and API navigation progress logic uses monthly-only quota state for that account

### Requirement: Monthly quota remains visible in recent-trend displays

The account usage trend SHALL preserve the recent 7-day trend timeframe while identifying monthly-only quota lines as monthly quota.

#### Scenario: Monthly account trend labels the monthly line
- **WHEN** an account trend view renders a monthly-only free account
- **THEN** the trend legend identifies the quota line as `Monthly`
- **AND** the trend view still identifies itself as a 7-day trend

### Requirement: Zero-credit assigned accounts are omitted from 5h and weekly donut totals

Aggregate quota donuts SHALL omit assigned accounts whose visible assigned credits for the corresponding donut are zero.

#### Scenario: Zero-credit account does not contribute to donut totals
- **WHEN** an assigned account has zero visible credits for a 5h or weekly donut calculation
- **THEN** that account is excluded from the corresponding donut total and legend contributions

### Requirement: Account quota refreshes preserve visual continuity

Account-facing quota surfaces MUST retain the last valid percentage when a
refresh briefly reports an unknown or non-finite value. They MUST keep the
corresponding quota row mounted for the same bounded hold and MUST NOT render
the unknown refresh state as zero. When a later valid percentage arrives, the
visible number and bar MUST ease from the displayed value to the new value at
0.1 percent display resolution. Reduced-motion preferences MUST update the
value without animation. Raw percentages used by sorting and routing MUST stay
unchanged.

#### Scenario: Temporary unknown value does not drain the bar

- **GIVEN** an account quota row displays a valid remaining percentage
- **WHEN** a refresh temporarily reports that percentage as unknown or non-finite
- **THEN** the account card, account list row, and account detail usage panel keep the last valid percentage visible
- **AND** the quota row remains mounted
- **AND** the bar does not drain to zero

#### Scenario: Fresh percentage replaces the held value smoothly

- **GIVEN** an account quota row is displaying a valid or held percentage
- **WHEN** a later refresh reports a different valid percentage
- **THEN** the displayed number and bar ease to the fresh percentage
- **AND** the visible number can change in 0.1 percent increments
- **AND** the raw percentage used by sorting and routing is unchanged

#### Scenario: Reduced motion skips the transition

- **GIVEN** the user prefers reduced motion
- **WHEN** a fresh valid percentage replaces the displayed percentage
- **THEN** the quota surface displays the fresh percentage without animation


### Requirement: Subscription sources show actual remaining quota without mixing units

Accounts and Dashboard SHALL include registered subscription model sources,
including exhausted sources, using their actual provider quota windows.
Remaining percentage SHALL be computed from a known finite used ratio and
SHALL retain the provider window, reset time and observation time. Unknown
quota MUST remain unknown rather than zero or fully available. Provider
percentage quotas MUST NOT be added to Codex credit totals or converted into
invented credit balances. Visibility in monitoring MUST NOT bypass eligibility,
entitlement, authentication, billing or quota gates for client requests.

#### Scenario: Exhausted Claude source remains monitorable
- **GIVEN** a registered Claude source reports a used ratio of 1.0
- **WHEN** Accounts or Dashboard renders its quota
- **THEN** the source remains visible with zero remaining percentage for that window
- **AND** monitoring does not make the exhausted source eligible for inference

### Requirement: Subscription quota history contains observed samples only

Source quota trends SHALL use retained append-only provider observations,
preserve each window and model identity, and expose actual observation and
reset timestamps. A seven-day display MUST NOT fabricate historical samples
for a newly registered source. Missing samples SHALL remain missing, and
reauthorizing a source for a different principal SHALL clear that source's old
principal observations before presenting the new principal's trend.

#### Scenario: Newly registered source has no invented history
- **GIVEN** a source has one retained observation within the last seven days
- **WHEN** its recent quota history is displayed
- **THEN** only that actual sample is presented and missing history is disclosed


### Requirement: Private source monitoring snapshots

Source account-list, observed-usage and quota-history reads MUST use `Cache-Control: no-store`. Retained source data MUST be hidden after authorization failure or Accounts read permission loss until a new authorized read succeeds. Global visual privacy MUST mask source identities and duplicate identities contained in quota-group labels without changing raw routing or quota values.

#### Scenario: Authorized source monitoring is not stored by HTTP caches
- **WHEN** an authorized reader requests a source account-list, observed-usage or quota-history snapshot
- **THEN** the response includes `Cache-Control: no-store`
- **AND** enabling global visual privacy masks identity fields and their quota-group copies


### Requirement: Empirical token allocation estimates remain separate from official quota

Accounts and Dashboard SHALL show per-window token equivalents from retained raw provider observations and actual normalized gateway input-plus-output records in the same reset identity. The calculation SHALL use nonoverlapping intervals accumulating at least five percentage points and a utilization-weighted median. Complete estimates SHALL bound boundary-spanning calls from zero to full actual usage without time-prorating, require the entire boundary allowance to be at most ten percent of recorded interval tokens, and expose that allowance in the range. Model-scoped windows SHALL include only exactly proven model aliases or served IDs; cache and reasoning subtotals MUST NOT be added twice. Stale observations, unknown resets, insufficient positive movement, ambiguous identities and unsupported scopes SHALL remain explicitly unknown or insufficient.

The view SHALL label estimates as empirical gateway-observed token equivalents rather than official caps, credit balances or guaranteed bounds. Detailed evidence SHALL include token/request/interval counts, utilization delta, evidence time and observed interval estimate range. Shared identities SHALL be identified as mirrored capacity and MUST NOT be added together. Estimates MUST NOT affect routing, billing or existing Codex credit totals. Private estimation reads SHALL require Accounts read at all scope and use no-store; access loss or authorization failure SHALL remove cached estimates until a new authorized read succeeds.

If no complete interval qualifies but positive fully known usage remains, the view MAY show a separately labeled `recorded_lower_bound` equivalent. Missing usage MUST NOT be zero-filled; this mode SHALL keep the total-allocation upper bound unknown and MUST NOT be labeled a complete estimate or official capacity. Complete supported intervals SHALL take precedence over partial evidence. With no known positive matched usage, no numerical capacity SHALL be invented.

#### Scenario: Matched usage estimates allocation and remaining tokens
- **GIVEN** a current reset window has complete bracketed usage of 100000 tokens while utilization increases by 0.1
- **WHEN** its current utilization is 0.3
- **THEN** its empirical allocation is approximately 1000000 tokens and remaining estimate approximately 700000 tokens
- **AND** the provider quota percentage and official Codex credits remain unchanged

#### Scenario: Inadequate evidence stays explicit
- **GIVEN** a quota has no proven reset identity, no sufficient positive movement, or no known positive bracketed token usage
- **WHEN** its account is rendered
- **THEN** that quota displays its insufficiency reason without an invented capacity


#### Scenario: Continuous traffic has bounded observation-edge error
- **GIVEN** an interval has 1000 fully bracketed tokens, a 10-token call crossing its left edge, and a 10-token call crossing its right edge
- **WHEN** utilization increases by 0.2 and all usage is complete
- **THEN** the completion-dated estimate is approximately 5050 tokens with an empirical boundary range of 5000–5100
- **AND** larger edge uncertainty waits for a longer matched interval rather than assuming a uniform token rate

#### Scenario: Missing usage supports only a labeled recorded minimum
- **GIVEN** 1000 confirmed tokens and another request with missing usage in a 0.2 utilization interval
- **WHEN** no complete supported interval is available
- **THEN** the view may show a recorded-only minimum equivalent of approximately 5000 tokens
- **AND** its upper bound remains null and the view states that total allocation cannot be established
- **AND** the unknown request is not counted as consuming zero and no routing or billing policy changes

