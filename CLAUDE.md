# CLAUDE.md — Project Conventions for AI-driven Development

This file describes how Claude Code (and other AI coding agents) should work in this repository. Human contributors: treat this as a living cheat-sheet too.

## Project Overview

`resumable-upload` is a Python implementation of the [TUS resumable upload protocol v1.0.0](https://tus.io/protocols/resumable-upload.html). It ships both server and client components with **zero runtime dependencies** for the core path. Cloud storage backends (S3 / GCS / Azure) are opt-in via extras.

- **Status**: published on PyPI — `pip install resumable-upload`
- **Python**: 3.9 through 3.14 (keep 3.9 floor)
- **License**: MIT
- **Compliance matrix**: see `TUS_COMPLIANCE.md`

## Architecture (30-second tour)

```
resumable_upload/
├── __init__.py            — public exports (keep surface minimal)
├── exceptions.py          — TusHookError, TusCommunicationError, TusUploadFailed
├── checksum.py            — multi-algorithm checksum registry
├── fingerprint.py         — default SHA-256 full-file fingerprint
├── metrics.py             — zero-dependency Prometheus counter registry
├── asgi.py                — TusASGIApp (FastAPI / Starlette adapter)
├── cli.py / __main__.py   — `resumable-upload` CLI: `serve` (server) + `upload`/`download`/`info` (client)
│
├── server/
│   ├── __init__.py        — re-exports TusServer, TusServerCore, TusHTTPRequestHandler
│   ├── core.py            — TusServerCore (full TUS 1.0.0 implementation; exposes handle_request + handle_request_async)
│   ├── server.py          — TusServer(TusServerCore), the canonical class to instantiate
│   └── http_handler.py    — TusHTTPRequestHandler (sync http.server glue)
│
├── client/
│   ├── __init__.py        — re-exports TusClient, Uploader, UploadStats, AsyncTusClient
│   ├── client.py          — TusClient (mixes the three mixins below)
│   ├── _mixin_base.py     — _ClientAttrs: shared attribute / method shape used by mixins
│   ├── _protocol.py       — pure transport-agnostic helpers shared by sync + async clients
│   ├── protocol.py        — ProtocolMixin: encode_metadata, get_metadata, get_upload_info, get_server_info
│   ├── concatenation.py   — ConcatenationMixin: create_partial_upload, create_final_upload
│   ├── parallel.py        — ParallelUploadMixin: _upload_parallel
│   ├── uploader.py        — Uploader (low-level chunk control)
│   ├── stats.py           — UploadStats
│   └── aio/               — async client package (httpx, requires [async] extra)
│       ├── __init__.py    — re-exports AsyncTusClient, AsyncUploader
│       ├── _http.py       — lazy httpx import + request/error mapping
│       ├── client.py      — AsyncTusClient (async equivalent of TusClient)
│       └── uploader.py    — AsyncUploader (async chunk control)
│
├── storage/
│   ├── __init__.py        — re-exports Storage, SQLiteStorage, S3/GCS/Azure (lazy)
│   ├── base.py            — Storage ABC; sync interface + *_async surface (defaults to asyncio.to_thread)
│   ├── sqlite_storage.py  — SQLiteStorage (default)
│   ├── s3_storage.py      — S3 backend (optional, boto3)
│   ├── gcs_storage.py     — GCS backend (optional, google-cloud-storage)
│   └── azure_storage.py   — Azure backend (optional, azure-storage-blob)
│
├── url_storage/
│   ├── __init__.py        — re-exports URLStorage + 3 implementations
│   ├── base.py            — URLStorage ABC
│   ├── memory_url_storage.py
│   ├── sqlite_url_storage.py
│   └── file_url_storage.py
│
└── locks/
    ├── __init__.py        — re-exports LockBackend + implementations
    ├── base.py            — LockBackend ABC
    ├── memory_lock.py     — InMemoryLockBackend (default)
    └── redis_lock.py      — RedisLockBackend (optional, redis)

tests/                     — pytest, one test file per module
examples/                  — runnable Flask/FastAPI/Django integration examples
docs/                      — user-facing mkdocs site (do not repurpose)
.docs/                     — AI-only artifacts (gitignored) — see below
```

Legacy import paths are kept alive but emit ``DeprecationWarning`` on
first use, **deprecated in 0.0.6, scheduled for removal after 0.1.2**:
``resumable_upload.storage_s3``, ``storage_gcs``, ``storage_azure``,
``locks_redis``, and ``client.base`` are ``sys.modules`` aliases for the
new submodules. New code (and the project's own tests / examples / docs)
should import directly from the new packages; the aliases exist only to
give downstream users one release cycle to migrate.

## Tech Stack & Constraints

- Runtime: **stdlib only** for `resumable_upload.server`, `resumable_upload.client`, `resumable_upload.storage` (SQLite is stdlib).
- Never add a runtime dependency to the core without explicit user approval.
- Cloud backends use their official SDK (`boto3`, `google-cloud-storage`, `azure-storage-blob`) gated by `[project.optional-dependencies]` extras.
- Type checker: **ty** (Astral). Never reintroduce mypy — see `14798d2`.
- Linter/formatter: **ruff** (config in `pyproject.toml`). Line length 100.
- Test runner: **pytest** + **pytest-cov**. Fixtures live next to tests; no `conftest.py` sprawl.
- Package manager for dev: **uv** (`uv pip install -e .[dev]`). `pip` is also fine.

## AI-driven Development Protocol

This project is developed primarily with AI agents. Follow these rules:

1. **Plans live in `.docs/plans/`** (gitignored). Every non-trivial feature starts with a markdown plan in that folder. Plans are extremely detailed (TDD step-by-step, exact code, exact commands). See `2026-04-17-phase-a-tus-compatibility.md` for the style.
2. **Research lives in `.docs/research/`** (gitignored). Gap analyses, comparative studies, competitor reviews. Capture them so future sessions don't redo the same work.
3. **ADRs live in `.docs/decisions/`** (gitignored). Short markdown files capturing "why we chose X over Y" for decisions that are non-obvious from the code.
4. **Always read the current plan before touching code**. Don't re-derive the design; the plan is authoritative.
5. **Follow the plan's TDD cycle per step**: write failing test → run to confirm it fails → implement minimal code → run to confirm it passes → commit. One logical change per commit.
6. **Use subagent-driven execution for plan work** (`superpowers:subagent-driven-development`). Fresh subagent per task; controller provides full context; two-stage review (spec compliance → code quality) before marking complete.
7. **Never commit AI working artifacts** (`.docs/`, `.claude/`, `.omc/` are gitignored). If a piece of research or a decision deserves public history, surface it in a PR description or `TUS_COMPLIANCE.md` update.

## Workflow: Issues, Branches, PRs

Issue-first, sub-PR-per-task. Same workflow real OSS projects like `tusd` use.

### Umbrella RFC issues
- One per Phase or major feature.
- Body = scope summary + link to `.docs/plans/<filename>.md` + checkboxed task list.
- Label: `rfc`, and a phase label (e.g., `phase-a`).
- Title: `RFC: <feature name>` or `Phase A — TUS ecosystem compatibility`.

### Branches
- Always branch from `main`. Never commit directly to `main`.
- Naming: **conventional prefix + short slug**.
  - `feat/<slug>` — new feature (`feat/concatenation-extension`)
  - `fix/<slug>` — bug fix (`fix/chunks-completed-off-by-one`)
  - `chore/<slug>` — infra / tooling / refactor-only (`chore/ai-dev-scaffolding`)
  - `docs/<slug>` — docs-only (`docs/production-deployment-guide`)
- One PR per Task inside a Phase, not one giant PR.

### Commits
- **Conventional Commits**: `type(scope): imperative summary`.
  - Types: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`, `build`, `ci`.
  - Common scopes: `server`, `client`, `storage`, `cli`, `metrics`, `locks`, etc.
- Keep messages under 72 chars on the subject line. Body explains *why* if non-obvious.
- Examples from history:
  - `fix(storage): consistent completed flag contract across all backends`
  - `chore: replace mypy with Astral's ty for type checking`
  - `fix(client): chunks_completed off-by-one, eta_seconds sentinel value`

### Pull requests
- PR title = same Conventional-Commits format as the squash-merge commit.
- PR body: reference the umbrella issue with `Part of #<n>` (or `Closes #<n>` if single-PR).
- Include a testing-evidence section: `pytest -v` output excerpt, `ruff check` clean, `ty check` clean.
- Default merge strategy: **squash merge**. One logical change on `main` per PR.
- Never force-push to `main`. Never skip pre-commit hooks.

## Feature Surface Checklist (every new server/client capability)

A new option or behavior is not "done" until every surface that should expose
it does. Before marking a feature complete, walk this list and either wire it
up or note why it doesn't apply:

- [ ] **server / client** — the core implementation + its sync **and** async paths
- [ ] **CLI** — in `cli.py`: a `serve` flag (+ `_serve` wiring) for a server option a deployer sets, or an `upload`/`download`/`info` subcommand/flag for a client capability
- [ ] **docs** — every surface that describes the feature: the `docs/` mkdocs pages (usage/API/operations, incl. `docs/operations/cli.md`), the README (feature list, extension/compliance table, API-reference links, CLI section), and `TUS_COMPLIANCE.md` when it touches the wire protocol. Keep `README.ko.md` in sync with `README.md`.
- [ ] **release notes** — no manual `CHANGELOG.md`; notes are auto-generated on GitHub Releases from merged PRs. For any user-visible change, make sure the **PR title** is a clear Conventional-Commits summary so it reads well in the generated notes
- [ ] **tests** — unit coverage on both sync and async surfaces
- [ ] **interop** — a `tests/test_interop.py` case when it touches the wire protocol and a counterpart (tusd / tus-js-client / tus-py-client) supports it

Example gap this caught: server gained `cors_allow_credentials`, `cors_max_age`,
and `checksum_algorithms`, but the `serve` CLI never exposed them until later.

## Quality Gates

Before every commit:

```bash
pytest -v
ruff check resumable_upload tests
ruff format --check resumable_upload tests
ty check resumable_upload
```

Pre-commit hooks are configured in `.pre-commit-config.yaml`; they run the same tooling. Don't bypass with `--no-verify`.

## TUS Protocol Invariants (don't break these)

These are not style preferences — they are wire-protocol requirements:

1. **Wire compatibility**: the server must remain interoperable with `tus-js-client`, `tus-py-client`, `tusd`, and `uppy`. Test against at least one real TUS client (examples are in `examples/`).
2. **`Tus-Resumable` header**: required on every non-OPTIONS request and every response. Version `1.0.0`.
3. **Headers are case-insensitive** on input but returned in canonical case on output.
4. **Error status codes**: follow `TUS_COMPLIANCE.md` exactly. 460 for checksum mismatch is intentional (non-standard but widely used).
5. **`Upload-Offset` is append-only**. Once advanced, it never decreases. Never mutate a completed upload.
6. **Concurrent PATCH with stale offset** must return 409 (not silently accept). See the `update_offset_atomic` contract.

## Hooks (`TusServer` extension points)

Pre-hooks can reject requests by raising `TusHookError(status_code=…)`. Post-hooks' exceptions are caught and logged but don't affect the client response.

- `on_incoming_request(method, path, headers)` — pre, every request
- `on_upload_create(upload_id, metadata, upload_length) -> Optional[dict]` — pre, POST; return dict to replace metadata
- `on_chunk_received(upload_id, offset, chunk_size)` — post, every accepted PATCH chunk; raising `TusHookError` stops+deletes the upload (StopUpload)
- `on_upload_complete(upload_id, metadata, file_info) -> Optional[dict]` — post, after final chunk; return `{status_code, headers, body}` to customize the finishing response
- `on_before_terminate(upload_id)` — pre, client DELETE; raise `TusHookError` to veto
- `on_upload_terminate(upload_id)` — post, after DELETE

Out-of-band: `TusServer.terminate_upload(upload_id)` deletes + fires the post-hook, bypassing the veto.

## Don't Touch

- `.venv/`, `.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`, `htmlcov/`, `site/`, `uploads/`, `uploads.db` — generated / environment / test output. If one of these causes a failure, fix the cause, don't edit the file.
- Anything under `resumable_upload.egg-info/` — regenerated by packaging.
- `uv.lock` — let `uv` manage it (library project, so mostly unused but don't hand-edit).

## Useful Commands

```bash
# Install for development
uv pip install -e ".[dev,test,all-storage]"

# Run all tests with coverage
pytest --cov=resumable_upload --cov-report=term-missing

# Run a single test file
pytest tests/test_server.py -v

# Lint & format
ruff check --fix resumable_upload tests
ruff format resumable_upload tests

# Type check
ty check resumable_upload

# Build docs locally
mkdocs serve

# Build distribution
uv build
```

## When In Doubt

- **Design question**: check `.docs/plans/` first. If no plan covers it, stop and ask the human before inventing.
- **Wire-protocol question**: check `TUS_COMPLIANCE.md` and the TUS spec (`https://tus.io/protocols/resumable-upload.html`). Don't guess.
- **Style question**: match the nearest existing file in the same module.
- **Dependency question**: the default answer is "don't add one". If you must, add to an optional extra, not to core.
