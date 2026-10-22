# omp-codex-lb

Connect OMP to **your OmniRoute gateway** with one reusable plugin. It discovers your key's models and uses OMP's native inference transports, while independently retaining the legacy `codex-lb` provider. Adding OmniRoute does not repoint Codex LB or change model roles, defaults, or other providers. Contract tests use **OMP SDK 18.12.3**. A fresh **OMP 18.12.5** process with all global extensions completed a native Claude response; other host versions are not covered by that runtime check.

The package contains the TypeScript extension, release update checker, a trusted Codex metadata snapshot, and this guide. It needs an installed OMP host and an existing gateway; it does not install a server or require Python. Source and releases: **https://github.com/jung-geun/omp-codex-lb**. It is distributed through GitHub, not npm. The package name stays `omp-codex-lb` for existing installations.

The OmniRoute server must include the companion **build-time subscriptions extension**, not just this client plugin. In the parent checkout see [../omniroute-extension](../omniroute-extension); its planned standalone distribution is [jung-geun/omniroute-subscriptions](https://github.com/jung-geun/omniroute-subscriptions). It applies to an exact pinned upstream source before the server build; it is not a drop-in native runtime plugin. Ask your gateway administrator whether `/v1/omp/models` is available. The OMP package does not install the server extension or configure upstream accounts.

## Install from GitHub

```bash
omp plugin install https://github.com/jung-geun/omp-codex-lb.git
```

This tracks the repository's default branch. To pin a reviewed release instead:

```bash
omp plugin install 'https://github.com/jung-geun/omp-codex-lb.git#v1.2.0'
```

This checkout prepares **1.2.0**. The tag command requires that release to have been published; the prior release is `v1.1.1`, which does not include persistent `/omniroute-connect` setup. Do not assume unpublished source is an available release.

You may instead clone the public plugin repository and run `omp plugin install .` from its root. When working in the parent gateway checkout, use `omp plugin install ./integrations/omp`.

## Connect your OmniRoute gateway

Obtain a suffix-free **HTTPS origin** and a **gateway API key** from your gateway administrator or its APIs page. Use your own gateway URL, for example `https://your-gateway.example`. A key issued by a different gateway is a different credential even if its label matches. Never use a Codex/Claude OAuth token or a direct upstream provider API key here.

After installing, start OMP and enter this command **inside OMP**, using only the origin:

```text
/omniroute-connect https://your-gateway.example
```

This creates the active profile's `extensions/omniroute-v1.json` atomically with mode `0600`. The file contains only schema version, origin, and a key-variable **name**. A matching existing file is preserved byte-for-byte (permissions are restricted to `0600`), including its `keyEnv`. Invalid or conflicting existing profile/adjacent settings are refused, not replaced. Do not supply a key or extra arguments. Setup performs no discovery or inference and never writes `models.yml`, `config.yml`, roles, credentials, or another gateway's settings. No-argument `/omniroute-connect` remains read-only status/help.

Quit OMP, supply the gateway key in the environment inherited by the **new** process, then refresh models and restart. In macOS zsh or bash:

```bash
printf 'OmniRoute gateway API key: '
IFS= read -r -s OMNIROUTE_API_KEY
printf '\n'
export OMNIROUTE_API_KEY
omp models refresh
omp models omniroute --json
omp
```

Use `/model` to select an `omniroute/<id>` actually returned by your gateway, or start `omp --model omniroute/<id>` with a real ID substituted. `/omniroute-models` lists registered models and `/omniroute-quota` refreshes quota for the selected OmniRoute model. Existing/resumed sessions can retain their prior provider: restart alone does **not** migrate a persisted `codex-lb` selection or make a retired gateway available. Select an OmniRoute model or begin a new session deliberately. Discovery is not evidence of remaining quota or successful inference.

The command persists the origin, **not the key**. Each new shell/process must inherit the selected key variable, typically through your secret manager or deliberate shell setup. Missing/empty keys fail closed; they are never replaced with a literal variable name, another gateway's key, or upstream credentials. Do not paste keys into chat or slash commands.

OmniRoute synchronizes chat models from active Antigravity OAuth connections authorized for your gateway key, including Gemini, Gemini 4, Claude Opus/Sonnet and GPT-OSS when returned by that connection's live catalog. IDs retain their routing namespace, for example `omniroute/antigravity/claude-opus-5-5-medium`; Antigravity-hosted Claude does not become a native Claude subscription route. All these models use OMP's native OpenAI Chat Completions transport through the gateway, including tool-call continuation and declared image capabilities. Discovery and inference use the connection's same official IDE/CLI client profile; use the existing CLI profile option and resynchronize when Gemini 4 is available there but absent from IDE discovery. Non-chat surfaces and models without verified limits are omitted. Antigravity quota is unavailable in the OMP quota contract; native Codex/Claude quota is never substituted.

Antigravity model allowlists and key-level forced-model pins must use the `antigravity/<model>` routing namespace. An unqualified model pin is omitted because chat rewrites the request before authorization and may resolve that bare ID to a different provider. Keys restricted to the native Codex/Claude-only subscription policy do not receive Antigravity entries.

### Configuration precedence and conflicts

At startup, `OMNIROUTE_BASE_URL`, when present, overrides both settings files. On that environment path, `OMNIROUTE_KEY_ENV` selects the key variable and defaults to `OMNIROUTE_API_KEY`. Without `OMNIROUTE_BASE_URL`, settings beside the loaded extension take precedence over the active profile's `extensions/omniroute-v1.json`; that file supplies **both** origin and `keyEnv`, so `OMNIROUTE_KEY_ENV` alone does not override an existing file. Restart after changing any of these values.

Persistent setup refuses an origin that differs from `OMNIROUTE_BASE_URL`, and refuses existing files or active key-variable overrides that disagree with the saved connection. Unset or deliberately correct the conflicting override/file before setup; the command will not silently switch gateways or discard unknown settings. Adjacent legacy settings also must match; setup never changes a linked/shared package's adjacent settings. To choose a custom variable for a new profile, export `OMNIROUTE_KEY_ENV=MY_GATEWAY_KEY` **before** setup; supply that variable's value separately. Repeating setup preserves an existing custom `keyEnv`.

Example of the resulting profile JSON (no manual creation required):

```json
{"schemaVersion":1,"baseOrigin":"https://your-gateway.example","keyEnv":"OMNIROUTE_API_KEY"}
```

Gateway administrators control API-key scopes, model access, account entitlements, and routing. Use a least-privilege gateway key; setup does not grant or modify scopes. **Shared Account Quota** (`self:account-quota`) is an explicit opt-in to disclose shared pool usage, not a prerequisite for inference or a management permission. Do not enable it merely to make setup work. Refresh models and restart OMP after access changes.

### Environment reference

| Variable | Meaning |
| --- | --- |
| `CODEX_LB_BASE_URL` | Gateway HTTPS origin, such as `https://your-gateway.example`. Do not append `/v1` or `/backend-api/codex`; paths, userinfo, query strings, and fragments are rejected. A trailing `/` is accepted. |
| `CODEX_LB_API_KEY` | Gateway API key, used when no alternative key variable is selected. |
| `CODEX_LB_KEY_ENV` | Optional **name** of another environment variable holding the gateway key, not the key itself. Defaults to `CODEX_LB_API_KEY`. |
| `OMNIROUTE_BASE_URL` | Optional OmniRoute HTTPS origin, such as `https://your-gateway.example`, without a path suffix. Overrides saved settings and never changes Codex LB. |
| `OMNIROUTE_API_KEY` | Key issued by the independent OmniRoute gateway. It is not the existing Codex LB key. |
| `OMNIROUTE_KEY_ENV` | Optional name of another environment variable holding the OmniRoute gateway key. Defaults to `OMNIROUTE_API_KEY`. |

For an existing key variable:

```bash
export CODEX_LB_BASE_URL='https://your-gateway.example'
export CODEX_LB_KEY_ENV='MY_GATEWAY_KEY'
# Supply and export MY_GATEWAY_KEY using your secret manager or secure prompt.
omp
```

When `CODEX_LB_BASE_URL` is set, environment configuration takes precedence over legacy settings. Otherwise, the plugin first reads `codex-lb-v1.json` beside the loaded extension, then (if that file is absent) the active OMP profile's `extensions/codex-lb-v1.json` through OMP's official `getAgentDir()`. A legacy file supplies both origin and key-variable name; `CODEX_LB_KEY_ENV` does not override the legacy file on this path. The standalone package does not ship or copy settings files. Without any configured origin, the plugin loads connection/model help but does not register a gateway provider or quota polling. Restart OMP after changing the environment.

## Legacy Codex LB setup

Obtain the suffix-free HTTPS gateway origin and a **gateway API key** from your gateway administrator or its APIs page. Do not use a Codex/Claude subscription token or a direct upstream provider API key.

From the root of this cloned repository:

```bash
omp plugin install ./integrations/omp
```

OMP 18.12.3 treats a local path install as a link to that directory. Keep the clone at that path; you do not need `npm install`, gateway server code, or the Python installer. The install command changes OMP's plugin registration, not your `models.yml`, model roles, or other providers.

Provide the connection in the environment inherited by OMP:

```bash
export CODEX_LB_BASE_URL='https://your-gateway.example'
# Supply CODEX_LB_API_KEY through your shell's secure prompt or secret manager.
# For example, in bash (the value is not echoed or stored in shell history):
read -r -s -p 'Gateway API key: ' CODEX_LB_API_KEY; printf '\n'
export CODEX_LB_API_KEY
omp models refresh
omp models codex-lb --json
omp
```

Use the actual model IDs returned by your gateway. In OMP, run `/lb-connect` for connection information, `/lb-models` for available models, and `/model` to select one. You can also start a new session from the terminal, for example:

```bash
omp --model codex-lb/gpt-6.1-sol
```

That example works only when your key has access to that model. Model visibility is not evidence of remaining quota or a successful inference.

## Commands and model refresh

| Command | Effect |
| --- | --- |
| `/omniroute-connect [https://your-gateway.example]` | Without an argument, shows status. With one HTTPS origin, saves private profile connection settings without keys, discovery, or inference; restart required. |
| `/omniroute-models` | Lists OmniRoute models already registered in OMP; no network refresh. |
| `/omniroute-quota` | Refreshes the selected OmniRoute model's pool quota when permitted. |
| `/lb-connect` | Shows the configured origin, key-variable name and presence status, and setup instructions. It never displays the key and does not save credentials or connect a subscription account. |
| `/lb-models` | Lists models already available in OMP's `codex-lb` registry, with protocol and context/output limits. It is not a network refresh command. |
| `/lb-quota` | Refreshes quota for the selected `codex-lb` model without sending an inference request. Available after an origin is configured. |
| `/lb-update` | Checks the public latest stable GitHub release and shows manual update guidance; no installation or inference. |
| `/model` | OMP's native model selector; choose a model under `codex-lb`. |
| `omp models refresh` | Terminal command to force authenticated model discovery; then restart OMP. |
| `omp models codex-lb --json` | Terminal command to inspect OMP's available models for this provider. |

Both gateways read `/v1/omp/models` using your gateway key and retain the native protocol and limits supplied by the gateway. **OmniRoute uses that manifest alone**: a successful response, including an empty list, cannot be supplemented with native Codex IDs excluded by the server. Its server filters active authorized OAuth connections, synced visible models and per-key model permissions before returning Codex, Claude and Antigravity chat entries. Models without verified context/output limits are omitted.

Only the legacy **codex-lb** gateway also supplements its manifest with the authenticated `/backend-api/codex/models` catalog. For native Codex models whose gateway catalog omits output limits, `codex-defaults.json` supplies metadata snapshotted from the official **OMP 18.12.3 Codex defaults**, not guessed limits: see the version-pinned [bundled model catalog](https://github.com/can1357/oh-my-pi/blob/v18.12.3/packages/catalog/src/models.json) and [Codex provider rules](https://github.com/can1357/oh-my-pi/blob/v18.12.3/packages/catalog/src/compat/rules/providers/openai-codex.kdl). Only IDs observed in that native catalog are eligible, and observed gateway context limits are retained. Unknown IDs without verified output metadata are not invented or assigned generic limits. This metadata is not evidence of account entitlement or successful inference.

OMP's runtime model cache is normally 24 hours and is provider-name scoped, so force a refresh after gateway origin, key, account, model, or access changes. Refreshing does not rewrite `models.yml` or model roles. The host retains last-known/static fallbacks when discovery fails and reapplies explicit `models.yml` model overlays even after successful discovery; such entries are not proof of current server access. Without explicit static OmniRoute entries, a successful refresh follows the server manifest. Gateway authorization still applies to every inference request.

The quota status follows the selected model's provider, refreshes every 60 seconds, and clears when another provider is selected. `/lb-quota` requests `/v1/usage/quota?model=<id>` and shows details; it does not replace OMP's `/usage`. No extra automatic quota widget is added. Remaining percentages and account-pool equivalents are **not token credit, an official token cap, prepaid balance, or an API rate-limit allowance**. Missing, stale, or failed observations remain unknown/unavailable. Discovery and static fallbacks cannot bypass gateway authorization, assignments, subscription exhaustion, or billing policy.

On OmniRoute, enable **Shared Account Quota** for the gateway key in API Keys to opt in to pool quota disclosure. Disabling or revoking `self:account-quota` hides quota without granting management access or changing model permissions.

### How to place quota beside context length

OMP's normal status presentation shows the plugin's status without custom configuration. To put the quota inline immediately after context usage, merge this optional configuration into the active profile's `config.yml`, keeping all other settings and any unrelated `statusLine` fields:

```yaml
statusLine:
  preset: custom
  rightSegments:
    - usage
    - context_pct
    - status
    - cost
    - token_total
    - session_name
    - time_spent
  showHookStatus: false
```

The `status` segment carries the quota beside `context_pct`; `showHookStatus: false` avoids displaying the same status again on the separate hook-status line. This affects the presentation of other extension statuses too. If you already use a custom segment order, add or move `status` immediately after `context_pct` rather than replacing the rest of your layout. The plugin installer does not write this optional preference. Restart OMP after editing the configuration.

## How to preserve an existing helper installation

Existing users may keep `scripts/connect-omp.py`; it writes private atomic backups and preserves static model snapshots, model roles, other providers, and existing context/output overrides. The standalone plugin does not create those snapshots. Existing `models.yml` entries stay in place and can provide static fallbacks when discovery fails; server access and quota policy still apply.

Do not load both the helper's copied extension and this plugin: they register the same provider and slash commands. Before switching:

1. Back up your OMP configuration and move **only** the helper-installed `codex-lb-v1.ts` out of OMP's extension discovery paths. The default location is `~/.omp/agent/extensions/codex-lb-v1.ts`; use the corresponding directory if you use a custom profile. Keep the backup outside the extensions directory so it is not loaded. Also stop explicitly loading that copy with `-e` or a configured extension path, if applicable.
2. Retain `models.yml` (or `models.yaml`), `config.yml`, roles, other providers, credentials, and the active profile's `extensions/codex-lb-v1.json`. The plugin reads that legacy JSON if neither an environment origin nor adjacent settings are present, so an existing helper installation does not require new connection environment variables. Keep supplying its existing gateway key environment variable to OMP.
3. Install the package as above, refresh models, and start a new OMP process. Use `/lb-connect` to inspect the origin and key-variable name. To use environment configuration instead of retained legacy settings, export the **same** gateway origin with `CODEX_LB_BASE_URL` and the existing key-variable name with `CODEX_LB_KEY_ENV` if it is not `CODEX_LB_API_KEY`.

Running the helper again recreates its copied extension; do not do that while the package plugin remains enabled. To return to the snapshot installer, uninstall the package first, then restore the old extension or rerun the helper deliberately. Do not delete your model configuration to switch installation methods.

## Trust and key privacy

OMP plugins execute local code with your user's privileges. Review `codex-lb.ts` and trust the checkout before installation or updating it. Because local installs are linked, later edits to that checkout change what the next OMP process loads.

Connection settings are **profile-scoped**, not project-private. Global installs, shared OMP profiles, and multiple projects using the same `getAgentDir()` share `extensions/omniroute-v1.json` and provider-scoped discovery caches. A local linked plugin is also shared source: do not put personal connection JSON next to it. Use separate OMP profiles for independently configured gateways; review existing settings/environment before setup. The plugin does not choose or migrate session model selections.

The gateway receives the gateway key, inference requests, and model-specific quota queries. Trust its operator and use HTTPS with a valid certificate. Plain HTTP and credential-bearing URLs are rejected; do not disable TLS checks. Model metadata is validated against fixed native roots on the configured origin; arbitrary remote URLs, headers, and executable code from the manifest are not accepted.

Keep keys out of chats, slash-command arguments, committed files, URLs, screenshots, and shared logs. Use a secret manager or a secure shell prompt; OMP subprocesses inherit exported environment variables. The plugin's connection help reports only key presence, not its value. It does not perform upstream OAuth login or token refresh. Removing the plugin does not revoke your gateway key; revoke it separately in the gateway when needed.

## Updates and notifications

On session startup, the plugin checks the public GitHub latest stable release in the background at most once every 24 hours. A newer numeric version produces an OMP notification with the trusted release link and an update command. It does not replace code, execute a command, or interrupt inference. `/lb-update` performs an explicit check immediately. GitHub/network errors are silent during automatic checks and reported without raw server content during an explicit check.

Only the public GitHub release endpoint is contacted: no gateway API key, provider token, account identity, selected model, or gateway URL is sent. GitHub still receives the normal network request/IP. Local check state is stored in the active OMP profile. To disable automatic checks, export `CODEX_LB_UPDATE_CHECK=0` before starting OMP; explicit `/lb-update` remains available.

For an installation tracking GitHub's default branch:

```bash
omp plugin upgrade omp-codex-lb
# Restart OMP after the upgrade.
```

A tag-pinned installation stays pinned when upgraded. To move it to a reviewed release, install that published tag explicitly, e.g. `omp plugin install 'https://github.com/jung-geun/omp-codex-lb.git#v1.2.0'`. For a local-path installation, update your trusted clone with your normal source-control workflow and restart OMP; the link uses that source directly. No updater automatically changes the checked-out source.

Users can also subscribe on GitHub using **Watch → Custom → Releases** for notifications outside OMP.

### Maintainer: publish an update

Work in the standalone repository, review and test the changes, bump the stable version in `package.json`, commit, and push to `main`. The GitHub Actions workflow runs the tests and packages only the explicit public files. When that version has no release yet, it creates `v<version>` and attaches the `.tgz` package with generated release notes. An ordinary code push without a version bump does not create a new release notification. Existing releases are never overwritten.

```bash
bun install --frozen-lockfile
bun test
npm version patch --no-git-tag-version
git add package.json bun.lock codex-lb.ts update-check.ts README.md
git commit -m 'Release updated codex-lb plugin'
git push origin main
```

Stage the actual files changed by your update; the example is not an instruction to omit new tests. Releases require successful Actions and repository Actions write permission. Do not commit `codex-lb-v1.json`, gateway secrets, `.env`, or user-profile state. The parent gateway workspace is not the public repository.

## Remove

Inspect or remove the package registration:

```bash
omp plugin list
omp plugin uninstall omp-codex-lb
```

Restart OMP after removal. Uninstalling does not delete the cloned repository, profile connection JSON, old helper files, `models.yml`, model roles, or gateway credentials. Existing static models may remain selectable; remove their configuration only if you intentionally want to disconnect that provider. Revoke unwanted gateway keys separately.

## Troubleshooting

- **No OmniRoute models:** run `/omniroute-connect`, confirm the new process inherited the selected key variable, run `omp models refresh` from the same environment, and restart OMP. Select an `omniroute` model explicitly, especially in resumed sessions. For legacy Codex LB use `/lb-connect`. Installation alone does not grant gateway access.
- **Invalid or conflicting connection:** supply exactly one suffix-free HTTPS origin. Environment overrides and adjacent/profile JSON must agree for persistent setup; malformed or unknown JSON fields are rejected without overwriting the file. Correct the configuration deliberately, then restart. Missing/empty keys fail closed; supply the named gateway key to the new process rather than retrying inference blindly.
- **Duplicate command/provider registration:** disable or move the helper's `codex-lb-v1.ts` outside discovery paths; keep its JSON and model snapshots.
- **401/403 or exhausted quota:** check gateway key permissions, account/model assignments, and fresh provider quota. A cached model listing or static fallback does not override a refusal.
- **Unknown/unavailable quota:** confirm a `codex-lb` model is selected and the gateway supports model-scoped quota. Do not interpret missing observations as zero usage or available credit.

The legacy Python installer belongs to the parent gateway checkout and is not part of this standalone distribution. OMP's installation and upgrade syntax follow the [18.12.3 plugin CLI](https://github.com/can1357/oh-my-pi/blob/v18.12.3/packages/coding-agent/src/cli/plugin-cli.ts); package entry points use the host's `omp.extensions` manifest.
