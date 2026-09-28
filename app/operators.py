"""Operator accounts, sessions and the audit log (Step 11.3).

An operator is Syslab staff using the dashboard at /admin. They belong to no
tenant and act on all of them, which is the whole difference from a tenant
token: a tenant token answers "whose data is this", an operator session answers
"which person is doing this". The first is in app/tenancy.py, this is the
second, and they share only the control-plane file.

One role for everyone in the beta (decided 28 September). What stops that
being reckless is that every change made through the dashboard is written to
`audit_log` under a named person.

Passwords are hashed with the standard library's scrypt. Sessions are random
tokens stored only as SHA-256, the same rule tenant tokens follow: the disk
never holds anything that would let a stolen copy of control.sqlite3 sign in.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from app import config
from app.tenancy import TenancyError, _with, token_hash

# Lower-case, starts with a letter, short enough to type. It is shown in the
# audit log, so it should read as a person, not an id.
VALID_OPERATOR = re.compile(r"^[a-z][a-z0-9._-]{1,31}$")
MIN_PASSWORD = 12
MAX_PASSWORD = 256  # scrypt cost does not grow with length, but a body limit is still a limit

# scrypt at n=2**14, r=8: about 16 MiB and tens of milliseconds per attempt.
# The login throttle is the real defence against guessing; this is what is left
# if the file itself is stolen.
_N, _R, _P, _DKLEN = 2**14, 8, 1, 32


class OperatorError(Exception):
    """Something about an operator account a person can read and act on."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime | None = None) -> str:
    return (moment or _now()).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# passwords
# --------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        computed = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt),
            n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(digest)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(computed, bytes.fromhex(digest))


# Checked against when the username does not exist, so an unknown name costs
# the same scrypt as a wrong password and the response time says nothing about
# which names are real.
_DECOY = hash_password(secrets.token_urlsafe(16))


def _valid_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise OperatorError(f"A password needs at least {MIN_PASSWORD} characters.")
    if len(password) > MAX_PASSWORD:
        raise OperatorError(f"A password can be at most {MAX_PASSWORD} characters.")
    return password


def _valid_id(operator_id: str) -> str:
    cleaned = (operator_id or "").strip().lower()
    if not VALID_OPERATOR.match(cleaned):
        raise OperatorError(
            "A username is 2-32 characters: lower-case letters, digits, '.', '_' "
            "or '-', starting with a letter."
        )
    return cleaned


# --------------------------------------------------------------------------
# accounts
# --------------------------------------------------------------------------

def _public(row: sqlite3.Row) -> dict:
    """An operator as the dashboard may show it. The hash never leaves here."""
    return {
        "id": row["id"],
        "display_name": row["display_name"],
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
        "active": row["disabled_at"] is None,
    }


def create(operator_id: str, display_name: str, password: str,
           connection: sqlite3.Connection | None = None) -> dict:
    operator_id = _valid_id(operator_id)
    display_name = (display_name or "").strip() or operator_id
    password_hash = hash_password(_valid_password(password))
    conn, owned = _with(connection)
    try:
        if conn.execute("SELECT 1 FROM operators WHERE id = ?", (operator_id,)).fetchone():
            raise OperatorError(f"An operator called {operator_id!r} already exists.")
        conn.execute(
            "INSERT INTO operators (id, display_name, password_hash, created_at) VALUES (?,?,?,?)",
            (operator_id, display_name[:100], password_hash, _stamp()),
        )
        conn.commit()
        return get(operator_id, connection=conn)
    finally:
        if owned:
            conn.close()


def get(operator_id: str, connection: sqlite3.Connection | None = None) -> dict | None:
    conn, owned = _with(connection)
    try:
        row = conn.execute("SELECT * FROM operators WHERE id = ?", (operator_id,)).fetchone()
        return _public(row) if row else None
    finally:
        if owned:
            conn.close()


def list_all(connection: sqlite3.Connection | None = None) -> list[dict]:
    conn, owned = _with(connection)
    try:
        return [_public(r) for r in conn.execute("SELECT * FROM operators ORDER BY id")]
    finally:
        if owned:
            conn.close()


def set_disabled(operator_id: str, disabled: bool,
                 connection: sqlite3.Connection | None = None) -> dict:
    """Disabling ends every session the operator has, in the same transaction."""
    conn, owned = _with(connection)
    try:
        if get(operator_id, connection=conn) is None:
            raise OperatorError(f"No operator called {operator_id!r}.")
        conn.execute("UPDATE operators SET disabled_at = ? WHERE id = ?",
                     (_stamp() if disabled else None, operator_id))
        if disabled:
            conn.execute("DELETE FROM operator_sessions WHERE operator_id = ?", (operator_id,))
        conn.commit()
        return get(operator_id, connection=conn)
    finally:
        if owned:
            conn.close()


def set_password(operator_id: str, password: str,
                 connection: sqlite3.Connection | None = None) -> None:
    """A new password ends every existing session: whoever had the old one is out."""
    password_hash = hash_password(_valid_password(password))
    conn, owned = _with(connection)
    try:
        if get(operator_id, connection=conn) is None:
            raise OperatorError(f"No operator called {operator_id!r}.")
        conn.execute("UPDATE operators SET password_hash = ? WHERE id = ?",
                     (password_hash, operator_id))
        conn.execute("DELETE FROM operator_sessions WHERE operator_id = ?", (operator_id,))
        conn.commit()
    finally:
        if owned:
            conn.close()


def verify_login(operator_id: str, password: str,
                 connection: sqlite3.Connection | None = None) -> dict | None:
    """The operator if this is their password and they are active, else None.

    One None for every way of being wrong -- unknown name, wrong password,
    disabled account -- so the answer never tells a guesser which it was.
    """
    try:
        operator_id = _valid_id(operator_id)
    except OperatorError:
        check_password(password or "", _DECOY)
        return None
    conn, owned = _with(connection)
    try:
        row = conn.execute("SELECT * FROM operators WHERE id = ?", (operator_id,)).fetchone()
        good = check_password(password or "", row["password_hash"] if row else _DECOY)
        if not row or not good or row["disabled_at"] is not None:
            return None
        conn.execute("UPDATE operators SET last_login_at = ? WHERE id = ?", (_stamp(), operator_id))
        conn.commit()
        return _public(row)
    finally:
        if owned:
            conn.close()


def any_active(connection: sqlite3.Connection | None = None) -> bool:
    conn, owned = _with(connection)
    try:
        return conn.execute(
            "SELECT 1 FROM operators WHERE disabled_at IS NULL LIMIT 1"
        ).fetchone() is not None
    finally:
        if owned:
            conn.close()


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------

def start_session(operator_id: str, client: str | None = None,
                  connection: sqlite3.Connection | None = None) -> str:
    """A new session token. Returned once; only its hash is stored."""
    token = secrets.token_urlsafe(32)
    now = _now()
    expires = now + timedelta(hours=config.ADMIN_SESSION_HOURS)
    conn, owned = _with(connection)
    try:
        # Expired rows are swept here rather than by a timer: this is the one
        # moment a row is written, so the table cannot grow faster than logins.
        conn.execute("DELETE FROM operator_sessions WHERE expires_at < ?", (_stamp(now),))
        conn.execute(
            "INSERT INTO operator_sessions (session_sha256, operator_id, created_at, expires_at, client)"
            " VALUES (?,?,?,?,?)",
            (token_hash(token), operator_id, _stamp(now), _stamp(expires), (client or "")[:64]),
        )
        conn.commit()
        return token
    finally:
        if owned:
            conn.close()


def resolve_session(token: str, connection: sqlite3.Connection | None = None) -> dict | None:
    """The operator behind this session, or None if it is unknown, expired or disabled."""
    if not token or not isinstance(token, str) or len(token) > 200:
        return None
    try:
        conn, owned = _with(connection)
    except TenancyError:
        return None
    try:
        row = conn.execute(
            """
            SELECT o.*, s.expires_at AS session_expires
              FROM operator_sessions s JOIN operators o ON o.id = s.operator_id
             WHERE s.session_sha256 = ?
            """,
            (token_hash(token),),
        ).fetchone()
        if row is None or row["disabled_at"] is not None:
            return None
        if datetime.fromisoformat(row["session_expires"]) <= _now():
            return None
        return _public(row)
    finally:
        if owned:
            conn.close()


def end_session(token: str, connection: sqlite3.Connection | None = None) -> None:
    conn, owned = _with(connection)
    try:
        conn.execute("DELETE FROM operator_sessions WHERE session_sha256 = ?", (token_hash(token),))
        conn.commit()
    finally:
        if owned:
            conn.close()


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------

def audit(operator_id: str | None, action: str, target: str | None = None,
          detail: dict | None = None, client: str | None = None,
          connection: sqlite3.Connection | None = None) -> None:
    """Record one change. NEVER pass a secret in `detail`."""
    conn, owned = _with(connection)
    try:
        conn.execute(
            "INSERT INTO audit_log (at, operator_id, action, target, detail, client) VALUES (?,?,?,?,?,?)",
            (_stamp(), operator_id, action, target,
             json.dumps(detail, sort_keys=True, default=str)[:4000] if detail else None,
             (client or "")[:64]),
        )
        conn.commit()
    finally:
        if owned:
            conn.close()


def recent_audit(limit: int = 20, connection: sqlite3.Connection | None = None) -> list[dict]:
    conn, owned = _with(connection)
    try:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (int(limit),))
        return [
            {k: row[k] for k in ("at", "operator_id", "action", "target", "client")}
            | {"detail": json.loads(row["detail"]) if row["detail"] else None}
            for row in rows
        ]
    finally:
        if owned:
            conn.close()
