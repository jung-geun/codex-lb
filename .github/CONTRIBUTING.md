# Contributing to this fork

This repository extends [Soju06/codex-lb](https://github.com/Soju06/codex-lb) under its MIT license. The extension branch is `feature/subscription-gateway`; `main` retains the upstream fork lineage. The extension is based on the commit recorded in [`gateway/UPSTREAM_REVISION`](../gateway/UPSTREAM_REVISION), not every subsequent upstream change.

Create focused topic branches from `feature/subscription-gateway` and target that branch in pull requests. Do not force-push or merge newer upstream code without reviewing migrations, routing ownership, and compatibility. The application source and its original contribution conventions live in [`gateway/`](../gateway/) and [`gateway/.github/CONTRIBUTING.md`](../gateway/.github/CONTRIBUTING.md). Run application commands from `gateway/`, not the repository root.

Behavior changes require an OpenSpec proposal and requirements before implementation. Preserve accepted-request ownership and uncertain-completion fences: never automatically replay a request whose prior completion is unknown. Never use production credentials or a copied production database in tests.

The active wrapper-layout verification workflow is [fork-ci.yml](workflows/fork-ci.yml). It covers provider routing, quotas, monitoring, native Messages, model discovery, operation safety, the legacy OMP installer, the dashboard, the plugin, and fresh image startup on AMD64 and ARM64. Original upstream workflows remain under `gateway/.github/workflows/` for reference; GitHub does not run them there. The fork workflow is scoped verification, not a claim that every upstream test has passed.

Keep `.env`, credentials, database files, encryption keys, backups, traffic captures, and internal rollout diaries out of commits. Public requirements belong in `gateway/openspec/specs/`; local operational evidence under `gateway/openspec/changes/` is excluded by the root ignore rules. Review artifacts for private data before explicitly adding any excluded path.

The installable OMP plugin is maintained at [jung-geun/omp-codex-lb](https://github.com/jung-geun/omp-codex-lb). Keep the source snapshot under `integrations/omp/` synchronized when changing shared model, quota, or installer contracts. Plugin releases and update notifications come from that standalone repository; pushing this gateway repository does not release the plugin or deploy a running gateway.
