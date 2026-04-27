<!--
PR title must follow Conventional Commits — type(scope): imperative summary, ≤72 chars.
Types: feat | fix | chore | docs | refactor | test | build | ci
Example: fix(server): apply Upload-Expires to concatenated final uploads
-->

## Purpose

<!-- What this PR does and why. Link the related issue: "Part of #N" or "Closes #N". -->

## Test Plan

<!-- How you verified the change. Commands, scenarios, fixtures. -->

## Test Result

<!-- Output / evidence. pytest excerpt, ruff/ty status, before-after if relevant. -->

---

<details>
<summary>Checklist</summary>

- [ ] PR title follows Conventional Commits (`type(scope): ...`)
- [ ] `pytest -v` passes locally
- [ ] `ruff check` / `ruff format --check` / `ty check resumable_upload` clean
- [ ] Tests added for new behavior or regression
- [ ] No new core runtime dependencies (cloud SDKs go in optional extras)
- [ ] If wire/header/status-code behavior changed, `TUS_COMPLIANCE.md` updated
- [ ] Docs / examples updated if user-facing behavior changed

</details>
