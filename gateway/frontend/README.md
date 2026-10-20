# Frontend (Bun + Vite + React + TypeScript + SWC)

This frontend is built with Bun, Vite, React, TypeScript, and SWC.

## Prerequisites

- Bun 1.3+

## Setup

```bash
cd frontend
bun install
```

## Development

```bash
bun run dev
```

Vite dev server runs on port `5173` by default and proxies API routes to FastAPI:

- `/api/*`
- `/v1/*`
- `/backend-api/*`
- `/health`

## Build

```bash
bun run build
```

Production assets are emitted to `../app/static`.

## Accounts

The Accounts sidebar includes both Codex accounts and model sources. Add account
offers Codex OAuth/import, OpenAI / ChatGPT API, Claude API, Claude subscription,
and the remaining provider presets/custom endpoints. Claude subscription creation
hands off directly to the existing OAuth or credential-import dialog.

Official metered API sources keep catalog identity and protocol fields read-only.
Their editor selects one catalog model at a time for limits, capabilities, and
pricing. Drafts survive model selection without applying one model's values to
other rows. Unknown context/output limits must be explicitly configured before
the model can appear in OMP discovery; blank prices remain unknown.

The `selected` query parameter keeps Codex account IDs unchanged; model sources
use `source:<source-id>`. Source monitoring stays visible to readers, while writes
require both `accounts:write` and the session's global write grant.

Source detail separates provider quota observations from the rolling 24-hour
gateway usage summary (`GET /api/model-sources/{id}/usage`). Missing provider
balance, quota, usage, or pricing remains unknown; gateway traffic is not a
provider balance API, and API-equivalent subscription costs are not paid charges.


## Quality

```bash
bun run lint
bun run test
bun run test:coverage
```

