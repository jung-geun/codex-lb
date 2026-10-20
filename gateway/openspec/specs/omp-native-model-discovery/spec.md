# omp-native-model-discovery Specification

## Purpose
TBD - created by archiving change provider-scoped-client-quota. Update Purpose after archive.
## Requirements
### Requirement: Selected-model gateway quota display
The repository-owned OMP extension SHALL query the authenticated model-scoped quota endpoint for the currently selected codex-lb model, using its existing configured HTTPS origin and gateway-key environment reference. It SHALL use supported extension status/widget/command APIs, not patch installed OMP internals or override built-in `/usage`. It SHALL provide `/lb-quota` for explicit refresh without model inference and refresh observations periodically while a codex-lb model is selected.

The display SHALL identify the resolved Codex or Claude provider, report 5h/7d remaining percentages only when complete, and distinguish observed account-equivalents, reporting coverage and unknown pools. It SHALL NOT call short-window API token headroom or empirical token estimates official subscription quota. HTTP errors and invalid/stale responses SHALL clear prior quota and show unavailable/unknown without raw errors or credentials.

On selection change or shutdown the extension SHALL invalidate pending results. Late responses SHALL NOT restore another model/provider's quota. Selecting another provider SHALL clear the gateway quota display. Existing discovery, gateway authentication, GPT transport/context metadata and user model-role configuration SHALL remain unchanged.

#### Scenario: Codex to Claude to unrelated provider
- **WHEN** the selected model changes from a Codex model to a Claude model under codex-lb
- **THEN** the displayed quota changes to the Claude provider pool
- **AND** a late Codex result does not overwrite it
- **WHEN** the selected provider changes away from codex-lb
- **THEN** gateway quota status and widget are cleared

#### Scenario: Manual quota refresh performs no inference
- **WHEN** the user executes `/lb-quota`
- **THEN** the selected model's quota is refreshed and presented
- **AND** no model inference is dispatched by the command

### Requirement: Standalone OMP package
The integration SHALL be installable with OMP's plugin CLI from `integrations/omp`, without the Python installer or pre-existing model YAML. It SHALL accept `CODEX_LB_BASE_URL` as an HTTPS origin and `CODEX_LB_KEY_ENV` as a gateway-key environment reference, defaulting to `CODEX_LB_API_KEY`. Explicit environment origin SHALL take precedence over legacy settings. Without an origin it SHALL expose setup help without registering an unusable provider. Existing model roles, unrelated providers, credentials, and model overrides SHALL remain intact.

#### Scenario: Clean-profile model discovery
- **WHEN** a user installs the plugin and configures an authorized HTTPS gateway and key
- **THEN** authenticated manifest models are registered under codex-lb
- **AND** native Codex IDs absent from that manifest are supplemented only when both observed native context and trusted OMP18.12.3 output metadata are available
- **AND** an unknown native ID is not assigned invented token limits

#### Scenario: Connection and model guidance
- **WHEN** a user invokes `/lb-connect` or `/lb-models`
- **THEN** setup instructions or the available model IDs, protocols, and limits are presented without inference or disclosure of the key value

### Requirement: Context-adjacent compact quota
The plugin SHALL publish compact selected-provider quota through the official status API without an automatic detail widget. The distribution guide SHALL explain placing the `status` segment immediately after `context_pct` and suppressing the duplicate hook-status line while preserving other configuration. Manual `/lb-quota` SHALL retain detailed coverage and pool-equivalent information.

#### Scenario: Inline console quota
- **WHEN** the host configures context_pct followed by status and selects a codex-lb model
- **THEN** provider-specific quota renders alongside context usage
- **AND** no automatic detail widget duplicates the quota

### Requirement: Public release update notification
The standalone plugin SHALL have a public GitHub repository containing only reviewed distribution source, tests, documentation, license notices and release automation. Operational environment files, user settings, credentials and backups SHALL NOT be published.

The plugin SHALL check the fixed public latest-stable-release endpoint in the background no more than once per 24 hours per active profile, with bounded timeout/body and no redirects or authentication. It SHALL NOT transmit gateway URLs, keys, selected models or account data in this request. It SHALL validate stable numeric release versions and only notify on a strictly newer version. It SHALL NOT execute update commands or install remote code. Automatic errors SHALL NOT interrupt inference. CODEX_LB_UPDATE_CHECK=0 SHALL disable automatic checks; /lb-update SHALL explicitly check and report a sanitized result.

#### Scenario: New stable release
- **WHEN** a newer stable release exists and a daily check is due
- **THEN** OMP presents an update notice with a trusted release link and manual update guidance
- **AND** inference and installed source remain unchanged

#### Scenario: Versioned publication
- **WHEN** the maintainer pushes a new stable package version to main
- **THEN** release automation runs the regression suite before publishing the version tag and packaged asset
- **AND** an unchanged version does not overwrite an existing release

