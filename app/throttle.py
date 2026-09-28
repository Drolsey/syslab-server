"""The sign-in throttle, shared by every login on this server.

Moved out of app/main.py unchanged in Step 11.3, because the operator
dashboard's login (app/admin.py) needs the same throttle and cannot import
app.main: the server runs as `python -m app.main`, so importing it again would
load a second copy of the whole app. One throttle for both logins also means a
guesser cannot double their attempts by alternating between them.
"""

from __future__ import annotations

import time

from fastapi import Request

from app import config

MAX_FAILURES = 8
FAILURE_WINDOW_SECONDS = 900
# The most client addresses whose recent failures are remembered at once.
# There is no correct number; there is only "bounded" versus "not bounded".
MAX_TRACKED_CLIENTS = 4096

# A plain dict, NOT a defaultdict, and that is the fix rather than a style
# preference. Reading _failures[client] on a defaultdict CREATES the key, so
# the old code grew an entry for every address that ever tried to sign in,
# successful or not, and never removed one: the timestamps inside a key were
# pruned, the keys themselves never were. On a tailnet that is a leak slow
# enough never to matter. Facing the internet it is free memory exhaustion
# from an attacker who only has to vary their source address.
_failures: dict[str, list[float]] = {}


def _sweep_failures(cutoff: float) -> None:
    """Forget clients whose failures have all aged out, and cap what is left.

    Forgetting a throttle entry is always the safe direction: it gives an
    attacker nothing they did not already have by waiting out the window, and
    it can never lock out someone who belongs here.
    """
    for client in [c for c, times in _failures.items() if not any(t > cutoff for t in times)]:
        del _failures[client]
    if len(_failures) > MAX_TRACKED_CLIENTS:
        # Still over the cap, so somebody is deliberately varying their
        # address. Drop the least recently failing first.
        oldest_first = sorted(_failures.items(), key=lambda item: max(item[1]))
        for client, _ in oldest_first[: len(_failures) - MAX_TRACKED_CLIENTS]:
            del _failures[client]


def client_address(request: Request) -> str:
    """Who to hold the login throttle against.

    Behind Cloudflare Tunnel every request arrives from the cloudflared
    container, so `request.client.host` is one address for the whole internet.
    The throttle then counts the world's wrong guesses into a single bucket:
    eight from anybody locks out everybody, which turns a rate limit into a
    denial of service against the operator. Cloudflare puts the real address in
    CF-Connecting-IP, and it sets that header itself, discarding whatever the
    caller sent.

    Guarded by a setting rather than always trusted, because the danger runs
    the other way round when nothing is in front: a header anyone may set is a
    throttle anyone may evade by varying one string. TRUST_CLIENT_IP_HEADER is
    therefore off by default and is only true when the app is genuinely
    unreachable except through the tunnel -- which is the same condition
    docs/runbook.md makes the operator assert when they publish it.
    """
    if config.TRUST_CLIENT_IP_HEADER:
        forwarded = request.headers.get("cf-connecting-ip", "").strip()
        if forwarded:
            # One address, never a list: CF-Connecting-IP is a single value.
            # X-Forwarded-For is deliberately not read -- it is caller-appended
            # and the left-most entry is whatever an attacker typed.
            return forwarded[:64]
    return request.client.host if request.client else "unknown"


def _recent_failures(client: str) -> list[float]:
    cutoff = time.time() - FAILURE_WINDOW_SECONDS
    _sweep_failures(cutoff)
    recent = [t for t in _failures.get(client, []) if t > cutoff]
    # Only write back a key that has something in it. An empty list is the
    # same information as no key at all, and one of the two is unbounded.
    if recent:
        _failures[client] = recent
    else:
        _failures.pop(client, None)
    return recent
