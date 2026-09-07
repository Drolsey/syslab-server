# Changelog

Notable changes to `syslab-server`, newest first.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[Semantic Versioning](https://semver.org/), with one project-specific rule:

> **A model change is a changelog entry.** Swapping the served model, its quantisation or its
> context length changes what the system does, even when no line of code moves. It goes under
> Changed, with the old and new values, the same as any other behavioural change.

Two other things belong here that a code-only changelog would miss: **licence findings**,
because a dependency's terms can change without its version changing, and **anything that
alters an on-disk layout**, because that is what a restore from backup has to match.

---

## [Unreleased]

### Added
- `docs/architecture.md` — system overview for an engineer who did not build this. Keeps
  what exists and what is planned deliberately separate.
- `docs/licences.md` — the licence inventory. Every component carries either a primary
  source and the date it was read, or the word `unverified`.
- This changelog.
- `BOOTSTRAP_TENANT` documented in `.env.example`. It was read by `app/config.py` and
  documented nowhere, despite deciding which tenant owns this install's data.

### Security
- **Recorded, not yet fixed:** `GET /` and `GET /api/docs` are unauthenticated. Safe behind
  Tailscale, not safe once the server is publicly reachable. Fix scheduled with the public
  network path.
- **Recorded, not yet fixed:** the login throttle in `app/main.py` keeps an in-memory
  dictionary keyed by client IP, pruned by timestamp within a key but never by key. Bounded
  on a private tailnet, unbounded behind a public tunnel.

### Licence findings
- **PyMuPDF 1.24.10 is AGPL-3.0** or a commercial licence from Artifex. It is the PDF reader
  for the whole search path. The AGPL's network clause can reach a hosted service. No
  decision has been made; see `docs/licences.md`. This predates the current work.
- **Piper's licence changed under the project.** The original MIT repository is archived and
  development moved to a GPL-3.0 fork. Kokoro at Apache-2.0 is therefore the planned
  text-to-speech default.
- **Gemma 4 is Apache-2.0; Gemma 1 to 3 are not.** They use Google's custom terms, which
  require passing use restrictions to anyone you distribute to. The family name does not tell
  you the licence.
- **Tailscale's free plan is non-commercial.** The client is BSD-3-Clause, but the plan terms
  restrict it. Tailscale stays for operator access and will not be the production request
  path.

---

## Before this changelog

Versioning starts here. Work before this point is recorded in the commit history, which is
unusually detailed, and in `docs/plans/`. The milestones, for orientation:

**Step 1, the ownership boundary — complete.** Every byte the system stores or returns has an
owner, and the code cannot return one owner's byte to another. Added `app/context.py` (the
tenant in a `ContextVar`, with no default) and `app/tenancy.py` (the control plane: tenants,
SHA-256 hashed tokens, per-tenant database rows). Storage moved to one folder and one search
index per tenant, with a migration that runs dry by default and ships an undo manifest.
Ends with `scripts/check_isolation.py`, verified by removing the isolation and confirming 8
of its checks fail. Tenant deletion refuses an active tenant, requires the id typed twice,
refuses the bootstrap tenant, and moves documents aside rather than destroying them.

**The conftest guard.** Twice during Step 1 a test wrote into this install's real data
folders and nothing said so. Every test is now snapshotted around the real `data/`, `index/`
and `control/` directories, and a test that adds anything to them fails by name.

**Step 0, clean baseline.** Fifteen files that had been changed but never committed, gated on
two machines before being committed.

**Earlier.** Document search over PDFs and spreadsheets via SQLite FTS5; four read-only
PostgreSQL tools with a four-layer safety model; an in-memory job lane so slow work never
blocks a conversation; token authentication; the file tools; and the model benchmark harness.

### Not yet started

- **Step 2, the ingestion contract.** Designed in full in
  `docs/plans/step-02-ingestion-contract.md`, awaiting sign-off on its five decisions. It is
  the prerequisite for embeddings, vision and anything else that wants something out of a
  document.
- **Per-tenant database credentials.** `tenancy.database_for()` raises for any row it finds
  because no cipher was chosen. The table exists and nothing writes to it. Recorded as
  decision 4.5 in the Step 1 plan.
