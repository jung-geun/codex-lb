# omniroute-subscriptions

Dependency-free, offline **build-time source extension** for OmniRoute **3.8.52**, upstream commit **`edab7c3a59bf7d6f040d581dbda3a46adbee0d57`**. Package/CLI version: **1.0.0**. Requires Node.js 22 or later; building/running OmniRoute additionally requires the Node versions and native dependencies supported by that upstream release.

This is **not a dashboard-installable runtime plugin** and is not version-agnostic. The extension includes Next.js API routes, dashboard changes, and internal native-stream/completion protections that upstream runtime hooks cannot install. Apply the complete, generated source bundle to the pinned source tree and build OmniRoute normally. Do not copy individual payload files into a running server.

Public repository: <https://github.com/jung-geun/omniroute-subscriptions>

## What the source extension adds

- Codex and Claude native subscription routing with same-provider, exact-model subscription-first policy; explicitly configured paid API fallback remains last resort. Subscription-only keys cannot use paid fallback.
- Native accepted/ambiguous-request and continuation-ownership protections: unknown completion is not permission to replay an operation or switch account/model. This does not promise that every upstream disconnect can be recovered.
- Authorized OMP model discovery at `/v1/omp/models` and model-scoped real quota at `/v1/usage/quota`. Existing API-key provider/model/connection restrictions remain in force. **Shared Account Quota is opt-in per gateway key**, not silently enabled for clients; ordinary inference keys are not administrators.
- Existing Providers, API Keys, Costs, Logs, and Quota surfaces gain subscription controls and attributable usage. API-equivalent token-price estimates are **estimates, not subscription charges, prepaid balance, or official quota**. Missing price/usage remains unknown. Empirical token-capacity estimates use compatible observations and are display-only. Quota-consuming warm-up is distinct from metadata refresh and remains an explicit operator action/configuration.
- Native Claude catalog discovery can use verified canonical capabilities when the static registry lags, including account-advertised **Haiku 5.5**. Models with unknown context/output limits remain excluded; account availability is not inferred from a model name.
- Authorized Antigravity chat discovery uses the account's synced official IDE/CLI-profile catalog, known model metadata, and qualified `antigravity/<model>` IDs. This includes supported Gemini/Gemini 4, Claude Opus/Sonnet, and GPT-OSS chat models, not only a hand-picked model prefix. These use the Antigravity Chat Completions route, never native Claude subscription quota. Antigravity quota remains unsupported. Metadata discovery does **not** prove inference availability: generic upstream 429 `Resource has been exhausted` does not establish quota exhaustion versus temporary throttling. Previous verification established Gemini text/tool completion, not successful Antigravity Claude inference or full context-boundary execution.

OMP client integration is a separate package: <https://github.com/jung-geun/omp-codex-lb>. Install/configure it with **your** gateway URL and a gateway API key, not provider API keys or OAuth tokens. This source installer never configures a client, issues a key, rotates OAuth, or changes an account's client profile.

## Get the two public source trees

The release checkout contains `bundle/manifest.json` and content-addressed `bundle/blobs/<sha256>` objects for both installed payloads and original rollback baselines. Target paths remain in the manifest; hashed storage names ensure package managers cannot omit special filenames such as `.gitignore`. No installer dependency installation is required.

```sh
git clone --branch v1.0.0 --depth 1 https://github.com/jung-geun/omniroute-subscriptions.git
EXTENSION="$(cd omniroute-subscriptions && pwd -P)"

git clone https://github.com/diegosouzapw/OmniRoute.git upstream-omniroute
cd upstream-omniroute
git checkout --detach edab7c3a59bf7d6f040d581dbda3a46adbee0d57
TARGET="$(pwd -P)"

node "$EXTENSION/bin/cli.mjs" check --target "$TARGET"
```

Alternatively install a downloaded release package with `npm install --global ./omniroute-subscriptions-1.0.0.tgz` and invoke `omniroute-subscriptions` directly. The archive/package must already be present for offline installation. The CLI itself does not run npm, Git, network requests, host helper commands, dependency installation, builds, restarts, or database migrations. Git above is only one way to obtain pinned source; an exact upstream archive works too.

Use physical paths (`pwd -P`), including the extension checkout location. Symlinks in either root, any ancestor, payload path, source parent, owned source file, lock, or journal path are rejected. On macOS `/tmp` and `/var` may be symlink aliases; use their physical `/private/...` paths instead. The installer targets a **local POSIX filesystem** with hard links, atomic rename, fsync, and Unix modes; Windows/network filesystems are not supported by this transaction design.

## Stop, apply, build, restart

Before `apply` or `remove`, stop admission, let accepted requests finish, and stop the gateway, builds, editors/watchers, and every other process writing this source tree. Do not replay unknown-completion requests as part of maintenance. Back up your existing application database, encryption secrets, configuration, and deployment definition using your normal operational procedure.

```sh
node "$EXTENSION/bin/cli.mjs" apply --target "$TARGET"
```

Then use **the pinned upstream's existing build workflow**, from the target tree. For its normal npm source build:

```sh
npm ci
npm run build
npm start
```

Or build the upstream Dockerfile with the extension source included:

```sh
docker build -t omniroute-subscriptions:1.0.0 .
```

Restart/recreate your service through your existing deployment tooling, selecting that newly built image/artifact. Preserve the existing database/data volume, environment, encryption keys, ports, and secrets; do not recreate or delete a database volume. A prebuilt unextended upstream image does not contain these source changes. Review the pinned upstream's build/runtime prerequisites rather than assuming this installer provisions them. Native npm dependencies and container images may require network access even though the source installer does not.

## Commands and compatibility

```text
omniroute-subscriptions check  --target <physical-upstream-directory>
omniroute-subscriptions apply  --target <physical-upstream-directory>
omniroute-subscriptions remove --target <physical-upstream-directory>
omniroute-subscriptions --help
```

- `check` is read-only. It reports `ready` for the pinned baseline or `installed` for a complete journaled installation. Conflicts, locked targets, or interrupted transactions exit 1.
- `apply` first validates the manifest identity and path safety, **every payload and original checksum**, the target `package.json` version/name/checksum, every modified file's baseline checksum, and the absence of every added file. It performs no existing source writes before completing validation. A fully installed, unchanged journaled target is idempotent. Untracked/mixed source is rejected rather than adopted.
- Compatibility is bounded by the pinned `package.json` hash and **the full owned-file scope**, not by a Git executable or trusted version string alone. It does not attest the Git HEAD or inspect unrelated upstream files; obtain a pristine checkout/archive of the exact commit. Changing an owned file, even on the same nominal upstream version, is a conflict.
- `remove` validates the entire owned scope and all original backups **before** restoring anything. For a completed installation, every owned file must still match the installed hash and recorded mode; even manually reverting one file to baseline is an edit and refuses the entire remove. There is no `--force`.
- Preexisting file modes are preserved. New source files use manifest mode `0644`. Unrelated files, `.env`, settings, and database/data files are not installation targets. Created directories are only removed with a matching recorded filesystem identity and only if empty; unknown, replaced, or nonempty directories remain.
- Success exits 0, conflicts/operation failures exit 1, invalid CLI arguments exit 2. Output contains status/file counts or conflict paths, never source bytes, credentials, or stack dumps.

Library use is also available: `import { run } from 'omniroute-subscriptions'; await run('check', { target: '/physical/source/path' });`. The optional `bundlePath` supports isolated public fixtures; production callers should use the package's bundled default.

## Rollback and interrupted transactions

Stop the gateway and all writers first, then run:

```sh
node "$EXTENSION/bin/cli.mjs" remove --target "$TARGET"
```

Rebuild the restored upstream tree and restart through your existing deployment tooling. **Code rollback is not database rollback.** The installer never migrates, restores, or downgrades a database. If upstream startup/build workflows migrate data, assess compatibility and use your own verified database backup/restore procedure before running an older application.

Mutation is serialized by an exclusive local owner lock. Dead lock generations can be claimed by **`remove` only**; a live owner, inaccessible PID, or reused live PID remains locked. The installer uses fully written immutable owner records and hard-link claims, so recovering a dead owner does not unlink an active installer's lock. It does not protect against non-cooperating editors, mount changes, malicious concurrent filesystem mutation, or separate hosts/PID namespaces. Stop those writers rather than relying on the lock as a security sandbox.

A private mode-`0700` `.omniroute-subscriptions/` directory stores the durable transaction journal, mode-`0600` original backups, and staging files. Each owned source update uses a synced temporary file and atomic rename; the complete multi-file extension is **not globally atomic to readers**. An ordinary apply failure attempts guarded rollback. A crash can leave a journaled mixture of baseline and installed files, so never run or build a half-applied tree.

For interrupted `applying`/`removing` phases, `remove` recovers only if **every** owned path is a known before/after state and all backups pass integrity checks. Interrupted preparation is recoverable only with wholly unchanged baseline source and valid surviving backups. Any unknown source bytes, symlink, incompatible bundle, or corrupt backup refuses recovery without overwriting those files. Retain this exact package version for recovery. Inspect and resolve an actual conflict deliberately; do not delete the journal or force-remove a lock to hide it.

After successful removal the private journal/backups intentionally remain with a durable `removed` marker. `check` reports `ready`, repeated removal is safe, and a later apply reuses the metadata. Directory ownership receipts are cleared on completion; a repeated remove validates the baseline and backups but does not repeat directory cleanup, including for older completed tombstones with retained receipts. Keeping the journal/backups avoids a non-atomic metadata-deletion window after source rollback. Orphan staging/owner files from abrupt termination can also remain private/recovery-prefixed; the installer never recursively removes unknown files/directories. Backups contain only the public pinned source, not `.env`, settings, database contents, or credentials.

## Public development tests

From this package directory, with Node.js 22 or later:

```sh
node --test test/*.test.mjs
```

The tests generate tiny source trees and matching bundles in physical temporary directories. They exercise apply/remove behavior, conflicts, checksum tampering, symlink/path rejection, modes, private state, and interrupted-journal recovery without accessing operator credentials, personal configuration, or a running gateway. Release bundles are generated from the canonical source delta; payload copies are release artifacts, not a second independently maintained server tree.
