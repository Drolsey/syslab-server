# Step 11: The Operator Dashboard (LAN-only pre-production beta)

Status: **v2.2 — 11.0-11.6 BUILT, TESTED AND SECURITY REVIEWED, 28 September 2026;
committed and pushed as `442c75c`.** 11.7: documentation and security review done
(`docs/reviews/step-11-security-review.md`, no findings at the bar); code review and
deployment to the box still to do. Suite 741 passed, 1 skipped.

## Version history

| Version | Date | Change |
|---|---|---|
| v1 | 28 Sep 2026 | Full dashboard planned: companies, corpus, ingestion/jobs, access tokens, settings (three tiers) with restart, retrieval tester, chat playground, GPU/system monitor, operators and audit pages. |
| v2 | 28 Sep 2026 | **Scope cut to the minimal slice.** Retrieval tuning and LLM changes are blocked until the real client corpus arrives, and much of v1 would sit on internals that are still moving (document identity, new producers in Steps 6-10, `.env`/`models.toml` keys). v2 builds only what gets a client corpus into the pipeline and lets an operator see what the pipeline does with it. Everything else in v1 is kept, unchanged in intent, as **Step 11b (deferred)** at the end of this file, with the reason each item waits. |
| v2.1 | 28 Sep 2026 | **What the build changed, recorded rather than silently absorbed.** The operator CLI is `scripts/operator_account.py`: `operator.py` would shadow the standard library's `operator` module whenever the script runs. "Ingest outstanding" and "Rebuild company" are one button, **Ingest now**: both were the same folder-ingest job. The retrieval tester shows `found_by` per passage, not a rank per retriever: `/api/v1/retrieve` does not expose those ranks and the tester must equal it. `ingest.status()` gained an optional shared connection after listing 57 documents took 1.3 s (now 0.07 s). The test corpus is 57 documents, not 55. Trash is emptied by hand (no page). |
| **v2.2** | 28 Sep 2026 | **Security review recorded.** `/security-review` over the whole change found nothing at HIGH or MEDIUM with confidence 8 or above; the record is `docs/reviews/step-11-security-review.md`. It turned up one inaccurate claim in this plan's own reasoning: decision 3 excludes `172.16.0.0/12` as "Docker's range", but Docker's default pools also use `192.168.x.0/20`, which the default allow-list includes. Added to the risks below; not exploitable as shipped. |

## 1. Why this step exists

Every operator task on syslab-server is a terminal job today: companies and tokens through
`scripts/tenant.py`, a corpus by copying files into `data/<tenant>/`, ingestion through scripts
or `/api/ingest`. The page at `/` (`app/web/index.html`) is signed into with one tenant's token
and cannot manage more than one company.

The next real blocker is a client's corpus. When it arrives, someone has to load it (with its
folder structure), see what ingested and what failed, and check what retrieval returns for real
questions — which is also how a golden set gets built with the client. v2 is exactly that, and
nothing more.

Scope agreed with the user (28 September):
- Syslab operators only, LAN only. No tunnel, no client logins.
- Personal operator accounts, one role for everyone.
- Dashboard at `/admin`; the page at `/` is kept as it is.
- Cloud storage connectors stay with Steps 7 (credentials) and 8 (connectors).
- A company gets its own folder, and folders nest inside it.
- The test corpus becomes its own company first.

Step numbers already taken: 5 embeddings, 6 speech, 7 credentials, 8 connectors, 9 extraction,
10 vision. So this is **Step 11**.

## 2. Decisions

1. **Company = tenant.** No new concept. A company is created with an operator-chosen slug
   (validated by `context.VALID_TENANT_ID`) and its folder `data/<id>/` is created with it.
2. **A document's identity is its path relative to the company folder**, e.g.
   `contracts/contract_01.pdf`. Today every module keys on the bare `path.name`, so two `a.pdf`
   in different folders would collide. A file at the top level keeps its bare name, so this is
   **backward compatible: no migration** of manifest rows, index rows or `derived/` folders.
   In `derived/<tenant>/<producer>/` the relative path is stored as one folder level, escaping
   `%` to `%25` then `/` to `%2F`; a name with neither maps to itself.
3. **Admin surface isolation.**
   - Page at `/admin`, API at `/admin/api/*`. Session cookie `syslab_admin`, `Path=/admin`,
     `HttpOnly`, `SameSite=Strict`, so it can never authenticate `/v1`, `/api` or `/api/v1`.
   - Not mounted at all when `PUBLIC_MODE` is on.
   - Every request must come from `ADMIN_ALLOWED_NETWORKS` (default `127.0.0.0/8, ::1/128,
     10.0.0.0/8, 192.168.0.0/16`). `172.16.0.0/12` is **deliberately excluded**: Docker bridges,
     where a future `cloudflared` would come from. Uses `request.client.host`, never
     `CF-Connecting-IP`.
   - Every non-GET request must carry `X-Syslab-Admin: 1` (no CORS is configured, so a
     cross-site page cannot send it).
4. **Accounts, sessions, audit rows in `control/control.sqlite3`**, three additive tables;
   `SCHEMA_VERSION` stays 1 per the rule in `app/tenancy.py`. `hashlib.scrypt` with a
   per-password salt, minimum 12 characters. Session tokens `secrets.token_urlsafe(32)`, stored
   as SHA-256, 12-hour expiry. Every write through the dashboard records who, what and target —
   never a secret. (The audit *viewer* is 11b; the rows are written from day one because they
   cannot be recovered later.)
5. **No build step, no external requests.** Vanilla HTML/CSS/JS like the current page, inline
   SVG icons, system font stack. Works with no internet.
6. **Jobs stay in memory.** The ingest manifest is already the durable record of ready /
   outstanding / failed; a job lost to a restart is re-queued with "Ingest outstanding".

### Theme (from the logo)

| Token | Light | Dark | Logo colour |
|---|---|---|---|
| `--brand-navy` | #003285 | #003285 | the dot |
| `--primary` | #0042A5 | #33A9FF | royal chevron |
| `--accent` | #0094FA | #00D2FA | azure / cyan |
| `--highlight` | #72F7FA | #72F7FA | aqua (gradient only) |
| `--bg` / `--surface` | #F5F8FC / #FFFFFF | #07111F / #0D1B2E | |
| `--ink` / `--muted` / `--line` | #0B1B33 / #5A6B85 / #DCE5F0 | #E6EEF8 / #8FA3BF / #1C2E47 | |

Follows `prefers-color-scheme` with a manual toggle (localStorage). Aqua-to-royal gradient only
on the logo mark, the active sidebar item and the login panel. Sidebar ~240px, collapses to
icons below 1024px, hash routing. Login page: centred card, logo, subtle gradient backdrop.
Logo: the supplied 196px PNG at `app/web/admin/logo.png` (also the favicon); swap for an SVG
when one exists. Every text/background pair checked for WCAG AA in both modes.

## 3. Sub-steps (v2)

### 11.0 — This plan. DONE 28 September.

### 11.1 — Nested folders (backend only). DONE 28 September.
- `app/config.py`: `doc_id(path)` (relative POSIX path) and `derived_key(doc_id)`.
- `app/sources.py` `Files.list()`: recursive, skipping hidden files and folders.
- `app/ingest.py`: key on `doc_id`; `output_dir`/`forget`/`forget_missing` through `derived_key`.
- `app/search.py`, `app/passages.py`, `app/vectors.py`, `app/tools.py`: `path.name` to `doc_id`,
  folder walks to the source's listing.
- `app/main.py`, `app/plane.py`: `{name}` to `{name:path}`. OpenAPI path unchanged.
- **Gate:** two `a.pdf` in different folders coexist everywhere and deleting one leaves the
  other; traversal still refused; top-level names unchanged; full pytest, `check_api_compat`,
  `check_isolation`, `check_ingest`, `check_search --rebuild`, `check_retrieval` numbers
  unchanged.
- **Review:** database-agent treats `source` as opaque (`lib/services/retrieval-client.ts`) but
  its `sources` filter must use relative paths for nested documents. Whole-document search
  indexes `name`, so folder words become searchable there; passages are unaffected (`source`
  is `UNINDEXED`).

### 11.2 — Test corpus as its own company. DONE 28 September (laptop; not yet the box).
`scripts/seed_test_corpus.py`: tenant `syslab-test-corpus`, copies
`tests/fixtures/corpus/contracts/` and `formats/` (minus `sentinels.json`), then
`ingest.rebuild()`. Idempotent. Ground truth (`reference_text/`, golden/manifest/baseline JSON)
is not copied. **Gate:** 57 documents (52 contracts, 5 formats) with folder paths; `formats/format_corrupt.pdf` failed on
purpose; retrieval cites `contracts/contract_XX.pdf`.

### 11.3 — Operators, sessions, audit rows, admin router. DONE 28 September.
`app/operators.py`, `scripts/operator_account.py` (`new`, `list`, `disable`, `enable`,
`reset-password`), login throttle extracted unchanged from `app/main.py` into
`app/throttle.py`, `app/admin.py` (network guard, header check, async session dependency;
`login`, `logout`, `me`), config `ADMIN_ALLOWED_NETWORKS`, `ADMIN_SESSION_HOURS`.
**Gate:** hash round trip; disabled operator and expired session refused; public IP and
`PUBLIC_MODE` get 404; POST without header 403; admin cookie rejected on `/v1`, `/api`,
`/api/v1`; `check_gateway_isolation` passes; every mutating route writes an audit row.

### 11.4 — Shell UI and Overview. DONE 28 September.
Login page, sidebar, theme, router. Overview: app uptime and code fingerprint, chat and embed
reachability (vLLM `/v1/models`), job counts, company and document counts. **Gate (manual):**
both themes, 1280 and 1024px, keyboard reachable with visible focus, zero external requests.

### 11.5 — Companies and Corpus (the local connector, beta quality). DONE 28 September.
`app/corpus.py`: `tree`, `store` (per-segment cleaning, hidden segments refused, depth 16,
path 255, suffixes from `parse.handles()`, `MAX_UPLOAD_BYTES` streaming limit, conflict
skip/replace), `mkdir`, `trash` (to `TRASH_DIR/<tenant>/<timestamp>/`, then the existing
`forget_missing` sweeps), `retire_company_files` (moved out of `scripts/tenant.py`, used by
both). Companies page: counts, create, disable, enable, delete (disabled first, id typed to
confirm). Corpus page: tree, upload files and folders (one file at a time, one folder-ingest
job at the end), mkdir, download, trash, per-file state with the producer's error,
re-ingest, "Ingest outstanding", "Rebuild company", the company's own jobs. **Gate:** path
cleaning and sweep tests; manual upload of `tests/fixtures/corpus/` as a folder into a fresh
company matches 11.2.

### 11.6 — Retrieval tester. DONE 28 September.
Company, query, `k`, optional `sources` filter; calls the same function `plane.py`'s
`/retrieve` does, inside `use_tenant`. Shows passage text, source, chunk id, fused score,
per-retriever rank, `what_this_means`. **Gate:** equals `/api/v1/retrieve` for the same input.

### 11.7 — Reviews, documentation, deployment. Documentation and security review DONE (no findings at the bar); code review and deployment OPEN.
Security review (auth, sessions, throttle, CSRF, network guard, upload paths, trash, cookie
scope), code review, accessibility pass. Docs: `usage.md`, `runbook.md`, `architecture.md`,
`README.md`, `HANDOVER.md`, `CHANGELOG.md`, `graphify update .`. Deploy on the box, create the
first operator, accept from a second LAN machine.

## 4. Risks
- **Plain HTTP on the LAN.** Passwords and cookies can be sniffed. Before anything beyond the
  beta: TLS via reverse proxy, or LAN access over Tailscale.
- **Nested names reach database-agent** (see 11.1 review).
- **Docker can also use 192.168.x.** Decision 3 leaves out `172.16.0.0/12` as Docker's
  range, but Docker's default pools also hand out `192.168.x.0/20` once 172.17-31 are used,
  inside the default allow-list. Narrow `ADMIN_ALLOWED_NETWORKS` to the office subnet on the
  box (found by the security review, below the bar).
- **`tree()` walks the whole company folder per call.** Fine for thousands of files; cache or
  paginate when a real corpus needs it.

## 5. Verification (v2)
1. Full pytest, plus `check_api_compat`, `check_gateway_isolation`, `check_isolation`,
   `check_ingest`, `check_search --rebuild`, `check_retrieval` unchanged.
2. On the box: `scripts/operator_account.py new <you>`; from another LAN machine open
   `http://192.168.1.185:8080/admin`, log in, create `acme-beta`, upload
   `tests/fixtures/corpus` as a folder, watch ingestion finish (`format_corrupt.pdf` failed with
   its error), run the retrieval tester and get `contracts/...` passages.
3. Negative: Docker-bridge source or `PUBLIC_MODE=true` gives 404 on `/admin`; admin cookie to
   `/v1/models` gives 401; POST without `X-Syslab-Admin` gives 403; ninth wrong password
   gives 429.

---

## Step 11b — Deferred (from v1; kept so the ideas are not lost)

Each waits for the pipeline to settle: the real client corpus, and Steps 6-10 adding producers
and config. Pick them up in this order unless something moves one forward.

1. **Access tokens page.** Tenant tokens (`tenancy.issue_token`/`list_tokens`/`revoke_token`,
   shown once) and aliases (`link_alias`/`unlink_alias`/`list_aliases`). *Waits because*
   `scripts/tenant.py` covers it and it is rarely done. First to pick up if non-developers need
   to onboard database-agent tenants.
2. **Cross-company Ingestion & jobs page.** `jobs.Lane.snapshot(all_tenants=True)`, all jobs
   with progress and cancel, failed documents across companies. *Waits because* v2's
   per-company view covers one corpus at a time.
3. **Settings, three tiers (approved 28 Sep).**
   - Runtime-safe: companies, tokens, aliases, ingestion (fully editable).
   - Restart-needed: whitelisted `.env` keys (`MAX_UPLOAD_MB`, `JOB_*`, `LLM_TIMEOUT`,
     `MAX_TOOL_STEPS`, `LLM_THINK`, `EMBED_TIMEOUT`, `GATEWAY_RATE_LIMIT_PER_MINUTE`,
     `ADMIN_*`); `GATEWAY_TOKENS`/`RETRIEVAL_TOKENS` per entry, write-only with a 4-character
     hint; `models.toml` in a text area validated by `tomllib` and the role rules; atomic writes
     with backups under `control/backups/`; `RETRIEVAL_TOKENS` validated by
     `config._retrieval_tokens`. Restart-pending from startup hashes of both files; restart
     button sends the response then `os._exit(3)`, relying on the unit's
     `Restart=on-failure` (detect systemd via `INVOCATION_ID`; disabled otherwise).
   - Read-only: `APP_HOST`, `APP_PORT`, `PUBLIC_MODE`, `TRUST_CLIENT_IP_HEADER`, `*_DIR`, base
     URLs, and the vLLM/embed blocks of `docker-compose.yml`.
   *Waits because* the keys are still changing step to step, and this is the riskiest surface
   in the dashboard.
4. **GPU / system monitor.** `nvidia-smi --query-gpu=...` with a timeout, vLLM `/metrics`
   (KV-cache use, running/waiting) for chat and embed, disk use per root and per company,
   5-second refresh. *Waits because* `nvidia-smi` and the check scripts cover it today.
5. **Chat playground.** `agent.ask()` inside `use_tenant`, tool steps shown, with a banner that
   the agent's write tools save into that company's corpus. *Waits because* database-agent is
   the production chat interface and the better place to test end to end.
6. **Operators page and audit viewer.** Create, disable, reset password (ends that operator's
   sessions), change own password; audit log filtered by operator, action and date. *Waits
   because* `scripts/operator_account.py` covers accounts and the audit rows are already written.

Also out of both v2 and 11b: cloud connectors (Steps 7, 8), client logins, operator roles,
persistent jobs, rename/move of files, retiring the page at `/`, retrieval tuning.
