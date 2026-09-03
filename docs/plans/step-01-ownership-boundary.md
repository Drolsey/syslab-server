# Step 1: The Ownership Boundary

Status: PLANNED, NOT STARTED. Needs Amro's sign-off on the five decisions in section 4.
Written 3 September 2026.

---

## 1. What this step is

Every byte the system stores or returns must have an owner, and the code must be unable to
return one owner's byte to another.

That is the whole step. It adds no capability a user would notice. What it buys is that
every capability added after it (vision, extraction, voice, image generation) inherits
isolation for free instead of having it retrofitted.

## 2. What this step is not

Deliberately out of scope, so the step stays finishable:

- No user accounts with passwords. One token per tenant, and a `users` table that exists but
  is not yet used.
- No admin UI. Tenants are created by a script.
- No billing, quotas, or per-tenant GPU scheduling.
- No server-side conversation history. Today the browser holds it and passes it back on every
  request, which is a genuine simplification. It becomes tenant-scoped state the day it moves
  to the server, and that day is not this one.
- No change to how the model reasons, and no change to `app/agent.py` at all if the design in
  4.2 holds. Proving that is one of the gates.

---

## 3. What has an owner today: nothing

This is the honest inventory. Every item is a module-level global with exactly one value.

| Thing | Where | Today |
|---|---|---|
| Documents | `config.DATA_DIR` | One folder. `resolve_in_data_dir()` refuses escapes from it, and that is the only boundary in the system. |
| Search index | `config.INDEX_PATH` | One SQLite FTS5 file. Rows keyed by `name` alone, so two tenants with `invoice.pdf` are one row. |
| Jobs | `jobs.lane` | One in-memory `Lane`. Any caller can `GET /api/jobs/{id}` or cancel any job. Lost on restart. |
| Customer database | `config.DB_*` | One host, user and password in `.env`. |
| Authentication | `config.APP_TOKEN` | One shared secret. It proves someone may use the app; it identifies nobody. |
| Conversation | request body | Client-held, so no server state. |

The important consequence: **there is no code path today that could accidentally serve the
wrong tenant, because there is only one.** After this step there will be, which is why the
gate is adversarial rather than functional.

---

## 4. Five decisions

Each one is a fork with real alternatives. Recommendations are mine; the choice is yours.

### 4.1 What is a tenant?

| Option | What it means | Cost of getting it wrong |
|---|---|---|
| **a. Tenant = customer organisation** | Data belongs to the org. People are logins that map to one. | Low. Adding people later is additive. |
| b. Tenant = individual user | Data belongs to a person. | High. Two people at one company cannot share a folder without a migration that invents an org. |
| c. Two levels from day one | Org owns data, users belong to orgs, projects inside orgs | Medium. More schema than the product needs yet. |

**Recommendation: (a).** The data-owning unit is `tenant`. A `users` table exists with a
`tenant_id`, unused in Step 1. If your first customers turn out to be individuals, an
individual is simply a tenant with one user, and nothing has to change. This is the option
that costs one column now instead of a data migration later.

### 4.2 How does identity travel through the code?

This is the decision with the most consequences, and one option is ruled out by code you
already have.

| Option | How | Verdict |
|---|---|---|
| a. Add a `tenant` parameter to every function | `read_pdf(tenant, filename)` | **Ruled out.** `agent._coerce_arguments` computes required arguments from each tool's signature and reports them to the model by name. A `tenant` parameter would be demanded from the model, appear in error messages, and become something a small model tries to guess. That is a security boundary the model can see and touch. |
| **b. `contextvars`** | One `ContextVar` set per request. `config`, `tools`, `search`, `jobs` and `db` read it. Signatures unchanged. | **Recommended.** No tool signature changes, so `agent.py` is untouched and the model never sees the boundary. |
| c. A `Workspace` object | Tools become methods on a per-tenant object | Cleanest in the abstract. Rewrites `tools.py`, `search.py`, the registry and most of 201 tests. Right if this were greenfield; expensive now for the same guarantee. |
| d. One process per tenant | OS-level isolation | Strongest isolation there is, and the correct answer for a customer with a compliance requirement. Wrong default here: each process holds its own everything and the GPU is shared anyway, which destroys the cost-per-token argument the whole product rests on. |

**The one rule that makes (b) safe: an unset context is an error, never a default.**
`current_tenant()` raises if nothing set it. The failure mode of ambient context is a code
path that silently falls back to a default tenant and quietly serves the wrong data. Making
it raise converts that silent leak into a loud crash in a test.

I will also add a thin `workspace()` accessor around the context var, so that if you later
want option (c) the change is mechanical rather than a rewrite.

### 4.3 Where does tenant state live?

| Option | Cost | Verdict |
|---|---|---|
| a. Filesystem convention only (`data/<tenant>/`) | Zero new infrastructure | Not enough. Nowhere to put tokens, per-tenant database credentials, or a tenant's display name. |
| **b. A SQLite control-plane database** | One file, WAL, no new service | **Recommended for now.** |
| c. PostgreSQL control plane (your own instance) | A real database to run and back up | Right eventually, and it is the roadmap's Phase 02 answer. |

**Recommendation: (b) now, (c) when there is a second server or genuinely concurrent
writers.** With one condition attached: the schema is written in portable SQL with no
SQLite-only types, and every read and write goes through one module, `app/tenancy.py`. The
move to PostgreSQL then changes one file.

Call the file `control/control.sqlite3`, deliberately not inside `data/` and not inside
`index/`. `data/` is customer content and `index/` is declared disposable. The control plane
is neither: losing it loses every tenant's identity.

### 4.4 Is the search index one file or one column?

Worth separating from 4.3 because the failure modes differ sharply.

| Option | Failure mode |
|---|---|
| One index file, `tenant_id` column, every query filtered | One forgotten `WHERE` returns another customer's document text inside an answer. Silent, plausible-looking, and exactly the kind of bug that survives review. |
| **One FTS5 file per tenant** | A bug opens the wrong file and returns nothing, or raises. Loud, and never cross-customer. |

**Recommendation: one file per tenant**, `index/<tenant_id>.sqlite3`. The index is already
designed to be disposable and rebuildable from `data/`, so per-tenant files cost nothing to
create, nothing to migrate and nothing to delete. Deleting a tenant becomes: delete a folder,
delete a file, delete a row.

The cost is that a future cross-tenant admin search would have to open many files. That is a
report you do not have and may never want, and it is a poor trade against silent disclosure.

### 4.5 Per-tenant database credentials

| Option | Verdict |
|---|---|
| a. Keep one set in `.env` | That is not tenancy. Rejected. |
| **b. Store per tenant in the control plane, encrypted at rest** | **Recommended.** Key from `.env` now, an OS keystore or a secrets manager later. |
| c. Never store; the customer supplies them per session | Safest, and it makes scheduled or background work against their database impossible. |

**Recommendation: (b),** with the encryption key in `.env` and a comment saying plainly that
this protects against a stolen control-plane file and not against a stolen server. The
existing four-layer read-only design in `app/db.py` is unchanged and stays per connection.

---

## 5. The design, concretely

### Layout on disk

```
data/<tenant_id>/            customer documents, one folder per tenant
index/<tenant_id>.sqlite3    that tenant's FTS5 index, disposable
control/control.sqlite3      tenants, users, tokens, encrypted DB credentials
logs/                        unchanged
```

`<tenant_id>` is a short opaque slug, generated, not chosen: lower-case letters and digits,
no customer name in it. Two reasons. A folder name is not a place to leak a client list, and
a renamed customer should not mean a moved folder.

### Control-plane schema

Portable SQL, no SQLite-only types.

```sql
CREATE TABLE tenants (
    id           TEXT PRIMARY KEY,      -- opaque slug
    name         TEXT NOT NULL,         -- display name, changeable
    created_at   TEXT NOT NULL,
    disabled_at  TEXT                   -- soft delete; nothing is dropped by accident
);

CREATE TABLE tokens (
    token_sha256 TEXT PRIMARY KEY,      -- the token itself is never stored
    tenant_id    TEXT NOT NULL REFERENCES tenants(id),
    label        TEXT,                  -- "amro laptop", so one can be revoked alone
    created_at   TEXT NOT NULL,
    last_seen_at TEXT,
    revoked_at   TEXT
);

CREATE TABLE users (               -- exists in Step 1, used in a later step
    id           TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL REFERENCES tenants(id),
    email        TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE tenant_database (     -- the customer's own database, per tenant
    tenant_id    TEXT PRIMARY KEY REFERENCES tenants(id),
    host         TEXT NOT NULL,
    port         INTEGER NOT NULL,
    dbname       TEXT NOT NULL,
    username     TEXT NOT NULL,
    password_enc TEXT NOT NULL,        -- encrypted, never plaintext
    sslmode      TEXT NOT NULL DEFAULT 'require',
    updated_at   TEXT NOT NULL
);
```

**Tokens are stored as a SHA-256 hash, never in plaintext.** Lookup hashes the presented
token and finds the row, so the control-plane file stops being a master key if it is copied.
The final comparison still uses `hmac.compare_digest`.

### New modules

| File | Job |
|---|---|
| `app/tenancy.py` | The only code that touches the control plane. Create, look up, resolve a token, read and write tenant database credentials. |
| `app/context.py` | The `ContextVar`, `current_tenant()` which raises when unset, and `use_tenant(id)` as a context manager. |

### Changed modules

| File | Change |
|---|---|
| `app/config.py` | `DATA_DIR` and `INDEX_PATH` constants become `data_dir()` and `index_path()` functions that consult the context. `resolve_in_data_dir()` resolves inside the current tenant's folder. |
| `app/tools.py` | Stops importing the constants. Otherwise untouched. |
| `app/search.py` | `connect()` opens the current tenant's index file. |
| `app/jobs.py` | `Job` gains `tenant_id`. `snapshot`, `get` and `cancel` refuse jobs belonging to another tenant. The worker sets the context from the job before calling the handler. |
| `app/db.py` | Credentials come from the control plane via the context, not from module constants. |
| `app/main.py` | `require_auth` resolves the token to a tenant and sets the context for the request. |
| `app/agent.py` | **Nothing.** Proving that is a gate. |

---

## 6. Step by step

Eight sub-steps. Each is independently shippable, independently revertable, and has its own
gate. Nothing later depends on a decision buried in something earlier.

### 1.0 Control plane, inert

Write `app/tenancy.py` and the schema. Write `scripts/tenant.py` with `new`, `list`,
`show`, `disable`, and `token`. Creating a tenant prints its token once and never again.

Nothing in the running app imports this yet.

**Gate:** `scripts/check_tenancy.py` creates two tenants, issues tokens, resolves each token
back to the right tenant, refuses a revoked token, refuses an unknown one, and confirms no
plaintext token appears anywhere in the file. `pytest` unchanged at 201.

**Revert:** delete two new files.

### 1.1 The context, inert

Write `app/context.py`. `current_tenant()` raises `NoTenantError` when unset.

Nothing uses it yet.

**Gate:** unit tests for set, get, nesting, and that an unset read raises rather than
returning a default.

### 1.2 Tenant-aware paths

The largest mechanical change and the one that carries the most risk.

- `config.data_dir()` returns `DATA_ROOT / current_tenant()`.
- `config.index_path()` returns `INDEX_ROOT / f"{current_tenant()}.sqlite3"`.
- `resolve_in_data_dir()` resolves inside `data_dir()`, with the existing escape refusals
  intact and now also unable to escape into a sibling tenant's folder.
- `tools.py` and `search.py` call the functions instead of reading the constants.
- Every test gains a fixture that sets a context.

**Why the tests are the bulk of it:** all 201 currently assume one global folder. The fixture
is small, but touching every test file is where the hours go and where a careless edit hides.

**Gate:** 201 tests pass with the fixture. A new test asserts that calling any tool with no
context raises rather than falling back. A source check asserts `tools.py` and `search.py` no
longer reference the old constants at all, so a later edit cannot quietly reintroduce one.

### 1.3 Migrate the existing data

Your current `data/` and `index/` become tenant `default`.

`scripts/migrate_to_tenants.py` with `--dry-run` as the default and `--apply` required.
It moves `data/*` into `data/default/`, deletes the old index, rebuilds `index/default.sqlite3`
from the files, and prints a before-and-after file count that must match.

**The service must be stopped first.** Windows will not let you move a file the running app
has open, and a half-moved data folder is the worst possible state.

**Gate:** file count before equals file count after, the rebuilt index holds the same number
of documents as the old one did, and `check_tools` plus `check_search` pass against the
migrated layout.

**Revert:** the script writes an undo manifest. Moving files back is mechanical, and the
index is disposable either way.

### 1.4 Jobs

`Job` gains `tenant_id`, set at submit from the current context. `snapshot` returns only the
current tenant's jobs. `get` and `cancel` raise the ordinary not-found error for a job
belonging to another tenant, deliberately not a "forbidden", because "that job exists but is
not yours" is itself a disclosure.

**The single most likely bug in this whole step lives here.** A `ContextVar` is not inherited
by a thread you start. The worker thread must set the context from the job record before
calling the handler, and if it does not, the job runs with no context and raises, or worse,
inherits whatever the worker last had. The test for this must submit as tenant A, submit as
tenant B, and assert each job wrote into its own folder.

**Gate:** `check_jobs` extended with the two-tenant case.

### 1.5 Authentication

`require_auth` hashes the presented token, looks it up, and sets the context for the request.
An unknown or revoked token is the existing 401. The rate limiter stays as it is.

`APP_TOKEN` in `.env` keeps working as the token for the `default` tenant, so your own setup
does not break on the day this lands. It is removed in a later step, not this one.

**Gate:** `check_remote` extended. Tenant A's token cannot read tenant B's files, and the
error is a plain not-found.

### 1.6 Per-tenant database credentials

`db.py` reads connection settings from `tenancy.database_for(current_tenant())`. The `DB_*`
values in `.env` become the seed for tenant `default`. The four-layer read-only design is
untouched.

**Gate:** `check_database` runs against the `default` tenant and returns the same 22 of 22.
A second tenant with no credentials configured produces a clear "no database configured for
this tenant" rather than silently using someone else's.

### 1.7 Confirm the agent is untouched

`git diff` on `app/agent.py` must be empty for the whole step. If it is not, decision 4.2
leaked into the model's view of the world and needs revisiting rather than patching.

### 1.8 The adversarial gate

This is the gate for Step 1 as a whole, and the reason the step exists.
`scripts/check_isolation.py`, two tenants, every one of these must hold:

1. A uploads `secret.pdf`. B's `list_files` does not show it. B's `search_files` does not
   find its contents. B's `read_pdf("secret.pdf")` says no such file.
2. B asking for `../<A's id>/secret.pdf`, and the Windows-style backslash form, are both
   refused by path resolution.
3. B cannot see, fetch or cancel A's job by id, and gets not-found rather than forbidden.
4. B's `run_sql` uses B's credentials even immediately after A's connection was used, so no
   pooled or cached connection crosses the boundary.
5. While the context is B, A's index file is never opened. Assert on the actual file path
   used, not on the results.
6. Every tool called with no context raises. No default tenant exists at any layer.
7. Deleting tenant B removes its folder, its index and its rows, and leaves A untouched.

**A check that only proves A works is not this gate.** Each of the seven must fail loudly if
the isolation is removed, so each gets written by first breaking the thing on purpose and
confirming the check catches it.

---

## 7. Risks, named

| Risk | Why it bites | Mitigation |
|---|---|---|
| `ContextVar` and threads | The job worker does not inherit the request's context. Silent wrong-tenant writes. | 1.4's two-tenant test, and the worker sets context explicitly from the job. |
| A forgotten call site | One place still reads `config.DATA_DIR` and serves the wrong folder. | The source check in 1.2 that forbids the constants outright. |
| Migration on Windows | Files held open by the running service. | Service stopped, dry run first, count matched after. |
| Test churn hiding a real change | 201 tests edited at once. | The fixture is one shared helper, applied mechanically, and the diff is read rather than trusted. |
| Scope creep into user accounts | Sessions, passwords and an admin UI are a week on their own. | 1.5 is a token-to-tenant lookup and nothing else. The `users` table stays unused. |
| Encryption key handling | An encrypted password with the key beside it in `.env` is thin. | Stated plainly in the code comment, and named as the first thing to move to a real secrets manager. |

---

## 8. Effort

Three to five working sessions, with 1.2 the largest by some distance because of the test
fixture, and 1.8 the most valuable. 1.0, 1.1 and 1.7 are short.

## 9. Rollback

1.0 and 1.1 are new files and delete cleanly. 1.2 through 1.6 are one commit each and revert
individually. 1.3 is the only one that touches data, and it ships with an undo manifest. The
index is rebuildable from `data/` at every point, so it is never the thing that has to survive.

## 10. What comes after

Step 2, the ingestion contract: one pipeline where a file entering the system becomes text,
page images, chunks and extracted fields, with pluggable producers. Image reading is then the
first new producer rather than a second parallel pipeline. It is written against tenant-scoped
storage from its first line, which is the entire point of doing this step first.
