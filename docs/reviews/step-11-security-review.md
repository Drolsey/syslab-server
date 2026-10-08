# Step 11 security review

| | |
|---|---|
| Date | 28 September 2026 |
| Change reviewed | Step 11 (v2): nested document folders (11.1), the test corpus as a company (11.2), and the LAN-only operator dashboard (11.3–11.6). Reviewed as uncommitted changes, which were then committed as `442c75c`. |
| Method | Claude Code `/security-review`: one pass over the full diff, every new file, and the existing code those files rely on. Three claims were then checked by hand before this record was written: how the tunnel is networked in `docker-compose.yml`, that no CORS middleware exists, and how uvicorn 0.32.0 handles proxy headers. |
| Reporting bar | HIGH or MEDIUM severity, at confidence 8/10 or above. The method's rules put denial of service, rate limiting, secrets stored at rest, missing hardening and documentation out of scope. |
| Result | **No findings at the bar.** Two notes below it: one follow-up open, one accepted. |

This is an automated code review, not a penetration test. Nothing was attacked on a running
server, and it says nothing about how the box itself is configured.

## What was checked, and why it holds

### Path traversal
- **The four routes that now take a path:** `/api/files/{name:path}`, `/api/ingest/{name:path}`, `/api/v1/documents/{name:path}` and `/api/v1/ingest/{name:path}`.
  - All four end in `config.resolve_in_data_dir`, either directly or through `sources.Files.fetch` or `tools._resolve`.
  - That function normalises backslashes, refuses `..` parts and drive prefixes, and resolves symlinks. It then requires the result to be inside the calling tenant's own folder.
  - The server decodes `%2F` before routing, so an encoded `../` still reaches the `..` check.
- **`config.doc_id()`** raises for a path outside the folder. It never falls back to the bare filename.
- **`config.derived_key()`:**
  - It reverses exactly, because `%` is escaped before `/`.
  - Every name it receives comes from `doc_id()` of a real file, never from a request or a model argument.
  - `ingest.forget()` rejects `..`, `.` and empty parts, and the key is always a single folder name.
- **Dashboard file operations** (`corpus.store`, `make_folder`, `trash`, `listing`, `download_path`):
  - `clean_relative` refuses `..` before any cleaning.
  - A segment that becomes `..` after Unicode normalisation starts with a dot, so it is dropped.
  - `:` and other unsafe characters are replaced.
  - Every result is checked again by `resolve_in_data_dir`, inside the context of a company that must already exist.
- **Deleted companies' files:** tenant ids must start with a letter, so no tenant can be named `_removed` and reach them.
- **The agent's write tools** now create parent folders, but only after the containment check.

### Reaching the dashboard
- **Public mode:** the router is not mounted when `PUBLIC_MODE` is on, and each request is also refused in that mode.
- **Routes without a session:** every data route requires an operator session. The only exceptions are:
  - the page;
  - the three listed assets;
  - login;
  - logout, which needs the session token itself.
- **Other credentials:** `app/admin.py` never reads tenant, service or gateway tokens. The `syslab_admin` cookie is scoped to `Path=/admin` and nothing else reads it. `tests/test_admin.py` asserts it opens nothing on `/v1`, `/api` or `/api/v1`.
- **Proxy headers:** uvicorn 0.32.0 is pinned in `requirements.txt` and handles proxy headers by default.
  - It trusts `X-Forwarded-For` only from 127.0.0.1 (`FORWARDED_ALLOW_IPS`), and uses the rightmost address it does not trust.
  - So a proxy on the box turns a request into its real public address, which the guard refuses.
  - A remote client cannot spoof its way into an allowed range, and a missing peer address fails closed.
- **The shipped tunnel:** `cloudflared` in `docker-compose.yml` has no `network_mode: host`.
  - It sits on the compose bridge network and reaches the app through `host.docker.internal:host-gateway`, so the app sees a 172.x container address, which is outside the default allow-list.
  - `docs/runbook.md` also requires `PUBLIC_MODE=true` whenever the tunnel is up.

### Requests from other sites, and cookies
- No CORS middleware is configured anywhere in `app/`, so another site cannot send the `X-Syslab-Admin` header without a preflight request, which fails.
- The cookie is `SameSite=Strict` and `HttpOnly`. No GET route changes anything.
- DNS rebinding reaches only the login page and the rate-limited login, because the cookie belongs to the real host name.

### Accounts and sessions (`app/operators.py`)
- Passwords use scrypt (n=2¹⁴, r=8, 16-byte random salt) and a constant-time comparison. An unknown username is checked against a decoy hash, so response time does not reveal which names exist.
- Session tokens come from `secrets.token_urlsafe(32)` and are stored only as SHA-256. Expiry and disabled accounts are checked on the server on every request.
- Disabling an account or changing its password deletes all of its sessions. Every query is parameterised.

### Cross-site scripting (`app/web/admin/admin.js`)
- Every server-supplied string that reaches `innerHTML` goes through `esc()`, which escapes `& < > " '`. That covers company names and ids, file and folder names, failure details, audit entries, model ids and errors, passage text, sources and chunk ids, job notes, and values restored from `localStorage`.
- The values that are not escaped are numbers, fixed lookup tables keyed by server-side values, or literals. Toasts and the login error use `textContent`.
- Downloads are sent as attachments, so an uploaded file is never rendered on the dashboard's origin.
- The tenant page at `/` is unchanged by this step and only ever puts fixed strings into `innerHTML`.

### Other
- **Tenant isolation:** the manifest, indexes, vectors and derived folders all stay per tenant. `jobs.Lane.counts()` returns numbers only.
- **The sign-in throttle** moved to `app/throttle.py` unchanged, and `app/main.py` re-exports the same objects.
- **The scripts** are command-line only, with no hardcoded credentials. `--generate` draws its password from `secrets`.

## Below the bar

### 1. Docker can also hand out 192.168.x addresses — OPEN
`app/config.py`, `.env.example` and `docs/runbook.md` all say Docker bridge networks live in
`172.16.0.0/12`, and that is why that range is left out of `ADMIN_ALLOWED_NETWORKS`. But
Docker's default address pools also include `192.168.0.0/16`, in /20 blocks, once the
172.17–172.31 ranges are used up. Those blocks fall inside the default allow-list.

This is not exploitable as shipped. All three of these would have to be true at once:
- the box has more than about fifteen Docker networks;
- `PUBLIC_MODE=false` while the tunnel is up;
- the tunnel's WAF rule fails.

Follow-up:
- Correct the three comments.
- On the box, narrow the setting to the office subnet, for example
  `ADMIN_ALLOWED_NETWORKS=127.0.0.0/8,::1/128,192.168.1.0/24`. Only do this if every office
  machine that needs the dashboard is on that subnet.

### 2. Clients off the LAN can tell `/admin/api/*` exists — ACCEPTED
A client outside the allowed networks gets 405 (wrong method) or 422 (malformed body)
instead of 404. FastAPI checks both before the router's network guard runs. This reveals
only that the routes exist.

## Accepted risk, carried from the Step 11 plan
**Plain HTTP on the LAN.** Passwords and the session cookie cross the office network
unencrypted. Under this review's rules that is missing hardening rather than a finding, and
it is accepted for the beta. Before anything wider, put TLS in front or use the LAN over
Tailscale.

## Not covered
- Correctness: `/code-review` has not been run on this change.
- Vulnerabilities in third-party dependencies.
- The box's live configuration: the `.env` values `PUBLIC_MODE`, `ADMIN_ALLOWED_NETWORKS` and
  `TRUST_CLIENT_IP_HEADER`. Check these when deploying.
- database-agent.
