"""The control plane: who exists, and which token belongs to whom.

This is the ONLY module that touches control/control.sqlite3. Everything else
asks it. That is deliberate. The plan is to move this store to PostgreSQL when
there is a second server or genuinely concurrent writers, and confining every
statement to one file makes that a change to one file rather than a search
across the codebase. For the same reason the SQL below is ordinary portable
SQL, with no SQLite-only types: TEXT and INTEGER only, timestamps as ISO-8601
strings in UTC.

Where it lives matters. control/ is deliberately NOT inside data/, which is
customer content, and NOT inside index/, which this project declares
disposable and rebuildable. The control plane is neither. Losing it loses
every tenant's identity.

Two rules that are easy to lose later:

1. A TOKEN IS NEVER STORED. Only its SHA-256 is. Anyone who walks off with
   control.sqlite3 gets a list of hashes, not a set of keys.
2. EVERYTHING IS FAIL-CLOSED. A disabled tenant, a revoked token, an unknown
   token and a malformed token are all the same answer: no tenant. There is no
   path in here that returns a tenant "just in case".
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import CONTROL_PATH, ensure_control_dir
from app.context import BadTenantError, validate_tenant_id


class TenancyError(Exception):
    """Something about tenants, tokens or the control plane is wrong."""


# The version of the schema this code understands. Opening a control plane
# written by a NEWER version is refused rather than guessed at: a half-migrated
# tenant table is a worse outcome than a clear error.
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenants (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    disabled_at TEXT
);

CREATE TABLE IF NOT EXISTS tokens (
    token_sha256 TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL REFERENCES tenants(id),
    label        TEXT,
    created_at   TEXT NOT NULL,
    last_seen_at TEXT,
    revoked_at   TEXT
);

CREATE INDEX IF NOT EXISTS tokens_by_tenant ON tokens (tenant_id);

-- Filled in a later step, created now so there is one schema rather than a
-- migration for every sub-step.
CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL REFERENCES tenants(id),
    email      TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_database (
    tenant_id    TEXT PRIMARY KEY REFERENCES tenants(id),
    host         TEXT NOT NULL,
    port         INTEGER NOT NULL,
    dbname       TEXT NOT NULL,
    username     TEXT NOT NULL,
    password_enc TEXT NOT NULL,
    sslmode      TEXT NOT NULL DEFAULT 'require',
    updated_at   TEXT NOT NULL
);
"""

# Generated ids avoid i, l, o, 0 and 1. Nothing reads an id aloud, but you will
# type one into `py scripts\tenant.py show <id>` and those six characters are
# where a typo comes from.
_ID_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
_ID_LENGTH = 12

TOKEN_BYTES = 32  # secrets.token_urlsafe(32) gives 43 characters
_TOUCH_AFTER = timedelta(seconds=60)


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def token_hash(token: str) -> str:
    """SHA-256 of a token, hex. The only form that ever reaches the disk."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open the control plane, creating it if it is not there.

    `path` exists so the gate script and the tests can work on a throwaway file
    instead of the real one. Nothing in the app passes it.
    """
    if path is None:
        ensure_control_dir()
        path = CONTROL_PATH
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
    except sqlite3.OperationalError:
        # WAL needs shared memory and fails on some network and container
        # mounts. Losing it costs concurrency, not correctness.
        pass

    # synchronous=FULL rather than search.py's NORMAL on purpose: the search
    # index can be rebuilt from the files on disk, and this cannot.
    try:
        connection.executescript(SCHEMA)
    except sqlite3.OperationalError as exc:
        connection.close()
        raise TenancyError(
            f"Could not open the control plane at {path}: {exc}. Set CONTROL_DIR "
            "in .env to a local disk; network shares and some container mounts "
            "cannot host a SQLite database."
        ) from exc

    _check_version(connection)
    return connection


def _check_version(connection: sqlite3.Connection) -> None:
    row = connection.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        connection.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        connection.commit()
        return
    try:
        found = int(row["value"])
    except (TypeError, ValueError):
        raise TenancyError(
            f"The control plane has an unreadable schema_version ({row['value']!r}). "
            "Refusing to touch it."
        ) from None
    if found > SCHEMA_VERSION:
        raise TenancyError(
            f"This control plane is at schema version {found} and this code "
            f"understands {SCHEMA_VERSION}. It was written by a newer build. "
            "Refusing to open it rather than risk half-migrating your tenants."
        )


def _with(connection: sqlite3.Connection | None):
    """Use the caller's connection, or open and close our own."""
    if connection is not None:
        return connection, False
    return connect(), True


def _row_to_tenant(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "created_at": row["created_at"],
        "disabled_at": row["disabled_at"],
        "active": row["disabled_at"] is None,
    }


# --------------------------------------------------------------------------
# tenants
# --------------------------------------------------------------------------

def new_id(connection: sqlite3.Connection | None = None) -> str:
    """An opaque id, not derived from the customer's name.

    A folder name is not a place to leak a client list, and a customer who
    renames themselves should not mean a moved folder.
    """
    conn, owned = _with(connection)
    try:
        for _ in range(50):
            candidate = "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_LENGTH))
            exists = conn.execute(
                "SELECT 1 FROM tenants WHERE id = ?", (candidate,)
            ).fetchone()
            if exists is None:
                return candidate
        raise TenancyError("Could not find an unused tenant id in 50 attempts.")
    finally:
        if owned:
            conn.close()


def create_tenant(name: str, tenant_id: str | None = None,
                  connection: sqlite3.Connection | None = None) -> dict:
    name = (name or "").strip()
    if not name:
        raise TenancyError("A tenant needs a name.")

    conn, owned = _with(connection)
    try:
        if tenant_id is None:
            tenant_id = new_id(conn)
        else:
            # One rule for what an id may be, and it lives in context.py so that
            # the control plane and anything that turns an id into a path cannot
            # drift apart. Refused, never clamped: silently changing an id the
            # caller chose means they hold one string and the store holds another.
            try:
                tenant_id = validate_tenant_id(tenant_id)
            except BadTenantError as exc:
                raise TenancyError(str(exc)) from exc
        if conn.execute("SELECT 1 FROM tenants WHERE id = ?", (tenant_id,)).fetchone():
            raise TenancyError(f"A tenant with id {tenant_id!r} already exists.")

        conn.execute(
            "INSERT INTO tenants (id, name, created_at, disabled_at) VALUES (?,?,?,NULL)",
            (tenant_id, name, _now()),
        )
        conn.commit()
        return get_tenant(tenant_id, connection=conn)
    finally:
        if owned:
            conn.close()


def get_tenant(tenant_id: str, connection: sqlite3.Connection | None = None) -> dict | None:
    conn, owned = _with(connection)
    try:
        row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        return _row_to_tenant(row) if row else None
    finally:
        if owned:
            conn.close()


def list_tenants(include_disabled: bool = True,
                 connection: sqlite3.Connection | None = None) -> list[dict]:
    conn, owned = _with(connection)
    try:
        sql = "SELECT * FROM tenants"
        if not include_disabled:
            sql += " WHERE disabled_at IS NULL"
        sql += " ORDER BY created_at, id"
        return [_row_to_tenant(row) for row in conn.execute(sql)]
    finally:
        if owned:
            conn.close()


def set_disabled(tenant_id: str, disabled: bool,
                 connection: sqlite3.Connection | None = None) -> dict:
    """Soft delete. Nothing is dropped, and nothing on disk is touched.

    Disabling is instant and reversible; deleting a tenant's data is a separate,
    deliberate act that belongs to a later sub-step.
    """
    conn, owned = _with(connection)
    try:
        if get_tenant(tenant_id, connection=conn) is None:
            raise TenancyError(f"No tenant with id {tenant_id!r}.")
        conn.execute(
            "UPDATE tenants SET disabled_at = ? WHERE id = ?",
            (_now() if disabled else None, tenant_id),
        )
        conn.commit()
        return get_tenant(tenant_id, connection=conn)
    finally:
        if owned:
            conn.close()


def delete_tenant(tenant_id: str, connection: sqlite3.Connection | None = None) -> dict:
    """Remove a tenant and everything the control plane knows about it.

    REFUSES AN ACTIVE TENANT. Disabling first is a deliberate two step: the
    first is instant and reversible and takes access away immediately, the
    second is not reversible at all. Making one act do both would mean the
    moment you decide is the moment it is gone, and those are rarely the same
    moment.

    This removes ROWS. It does not touch the tenant's folder or index file:
    deciding what happens to a customer's documents is a separate judgement,
    made by scripts/tenant.py, which moves them aside by default rather than
    destroying them.
    """
    conn, owned = _with(connection)
    try:
        tenant = get_tenant(tenant_id, connection=conn)
        if tenant is None:
            raise TenancyError(f"No tenant with id {tenant_id!r}.")
        if tenant["active"]:
            raise TenancyError(
                f"Tenant {tenant_id!r} is still active. Disable it first, check that "
                "nothing broke, then delete it. Two steps on purpose: the first one "
                "can be undone and this one cannot."
            )

        counts = {}
        for table in ("tokens", "users", "tenant_database"):
            cursor = conn.execute(f"DELETE FROM {table} WHERE tenant_id = ?", (tenant_id,))
            counts[table] = cursor.rowcount
        conn.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))
        conn.commit()

        left = conn.execute(
            "SELECT count(*) AS n FROM tokens WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()["n"]
        if left:
            raise TenancyError(
                f"{left} token(s) for {tenant_id!r} survived the delete. Refusing to "
                "report success."
            )
        return {"tenant": tenant_id, "name": tenant["name"], **counts}
    finally:
        if owned:
            conn.close()


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------

def issue_token(tenant_id: str, label: str | None = None,
                connection: sqlite3.Connection | None = None) -> str:
    """Create a token for a tenant and return it. THIS IS THE ONLY TIME
    ANYONE SEES IT: the store holds the hash, so it cannot be shown again.
    """
    conn, owned = _with(connection)
    try:
        tenant = get_tenant(tenant_id, connection=conn)
        if tenant is None:
            raise TenancyError(f"No tenant with id {tenant_id!r}.")
        if not tenant["active"]:
            raise TenancyError(
                f"Tenant {tenant_id!r} is disabled. Enable it before issuing a token, "
                "so a token is never handed out for an account that cannot be used."
            )
        token = secrets.token_urlsafe(TOKEN_BYTES)
        conn.execute(
            "INSERT INTO tokens (token_sha256, tenant_id, label, created_at, "
            "last_seen_at, revoked_at) VALUES (?,?,?,?,NULL,NULL)",
            (token_hash(token), tenant_id, (label or "").strip() or None, _now()),
        )
        conn.commit()
        return token
    finally:
        if owned:
            conn.close()


def list_tokens(tenant_id: str | None = None,
                connection: sqlite3.Connection | None = None) -> list[dict]:
    """Tokens as metadata. There is no way to return the token itself."""
    conn, owned = _with(connection)
    try:
        if tenant_id is None:
            rows = conn.execute("SELECT * FROM tokens ORDER BY created_at")
        else:
            rows = conn.execute(
                "SELECT * FROM tokens WHERE tenant_id = ? ORDER BY created_at",
                (tenant_id,),
            )
        return [
            {
                "fingerprint": row["token_sha256"][:12],
                "tenant_id": row["tenant_id"],
                "label": row["label"],
                "created_at": row["created_at"],
                "last_seen_at": row["last_seen_at"],
                "revoked_at": row["revoked_at"],
                "active": row["revoked_at"] is None,
            }
            for row in rows
        ]
    finally:
        if owned:
            conn.close()


def revoke_token(fingerprint: str, connection: sqlite3.Connection | None = None) -> dict:
    """Revoke one token by the fingerprint shown in `list_tokens`.

    A prefix is accepted because nobody wants to retype 64 hex characters, but
    an ambiguous prefix is refused rather than resolved to the first match:
    revoking the wrong customer's token is not a mistake worth being helpful
    about.
    """
    fingerprint = (fingerprint or "").strip().lower()
    if len(fingerprint) < 8:
        raise TenancyError("Give at least 8 characters of the token fingerprint.")

    conn, owned = _with(connection)
    try:
        rows = conn.execute(
            "SELECT * FROM tokens WHERE token_sha256 LIKE ? || '%'", (fingerprint,)
        ).fetchall()
        if not rows:
            raise TenancyError(f"No token whose fingerprint starts with {fingerprint!r}.")
        if len(rows) > 1:
            raise TenancyError(
                f"{len(rows)} tokens start with {fingerprint!r}. Give more characters."
            )
        row = rows[0]
        if row["revoked_at"]:
            return {"fingerprint": row["token_sha256"][:12], "already_revoked": True}
        conn.execute(
            "UPDATE tokens SET revoked_at = ? WHERE token_sha256 = ?",
            (_now(), row["token_sha256"]),
        )
        conn.commit()
        return {"fingerprint": row["token_sha256"][:12], "already_revoked": False}
    finally:
        if owned:
            conn.close()


# --------------------------------------------------------------------------
# a tenant's own database
# --------------------------------------------------------------------------

def database_for(tenant_id: str,
                 connection: sqlite3.Connection | None = None) -> dict | None:
    """The connection settings this tenant's database was configured with.

    None means none stored. It does NOT mean "use somebody else's": there is no
    fallback here, and the caller has to decide what to do with nothing.

    NOTHING WRITES TO THIS TABLE YET, on purpose. Storing one customer's
    database password is what .env already does; storing many is a different
    thing, and it needs an encryption decision that has not been made. Until it
    is, a row that somehow exists is refused rather than trusted, so the first
    thing that puts credentials in here has to deal with the question.
    """
    conn, owned = _with(connection)
    try:
        row = conn.execute(
            "SELECT * FROM tenant_database WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        if row is None:
            return None
        stored = row["password_enc"] or ""
        if not stored.startswith("enc:"):
            raise TenancyError(
                f"The stored database password for tenant {tenant_id!r} is not in a "
                "form this build recognises. Credential encryption has not been "
                "chosen yet, so nothing should have written it. Refusing to use it."
            )
        raise TenancyError(
            f"Tenant {tenant_id!r} has stored database credentials, but this build "
            "cannot decrypt them: no cipher is configured. See decision 4.5 in "
            "docs/plans/step-01-ownership-boundary.md."
        )
    finally:
        if owned:
            conn.close()


def has_any_active_token(connection: sqlite3.Connection | None = None) -> bool:
    """Could anyone sign in through the control plane?

    Used by the startup guard, which refuses to serve when nobody can. Any
    failure answers False, so a control plane that will not open makes the app
    refuse rather than open the door.
    """
    try:
        conn, owned = _with(connection)
    except Exception:  # noqa: BLE001
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM tokens t JOIN tenants n ON n.id = t.tenant_id "
            "WHERE t.revoked_at IS NULL AND n.disabled_at IS NULL LIMIT 1"
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        if owned:
            conn.close()


def resolve_token(token: str, connection: sqlite3.Connection | None = None,
                  touch: bool = True) -> dict | None:
    """Which tenant does this token belong to? None means no tenant.

    Unknown, malformed, revoked, and belonging to a disabled tenant all return
    None. The caller gets one answer to act on and learns nothing about which of
    those it was, because "that token exists but is revoked" is itself a fact
    worth not disclosing.
    """
    if not token or not isinstance(token, str):
        return None

    conn, owned = _with(connection)
    try:
        digest = token_hash(token)
        row = conn.execute(
            "SELECT * FROM tokens WHERE token_sha256 = ?", (digest,)
        ).fetchone()
        if row is None:
            return None
        # The lookup already matched on the full hash. compare_digest here is
        # belt and braces against a future edit that makes the lookup a prefix
        # or a LIKE.
        if not hmac.compare_digest(row["token_sha256"], digest):
            return None
        if row["revoked_at"]:
            return None

        tenant = get_tenant(row["tenant_id"], connection=conn)
        if tenant is None or not tenant["active"]:
            return None

        if touch:
            last = _parse(row["last_seen_at"])
            if last is None or datetime.now(timezone.utc) - last > _TOUCH_AFTER:
                # Throttled: without this every authenticated request becomes a
                # write, and SQLite serialises writers.
                conn.execute(
                    "UPDATE tokens SET last_seen_at = ? WHERE token_sha256 = ?",
                    (_now(), digest),
                )
                conn.commit()
        return tenant
    finally:
        if owned:
            conn.close()
