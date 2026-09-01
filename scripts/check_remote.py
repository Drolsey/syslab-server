"""Phase 06 gate: is the door locked, and is it reachable from your tailnet?

Run this on the desktop after setting a token and installing Tailscale.

    python scripts/check_remote.py

The last section prints the address to open on your laptop. The real gate is
doing that from a genuinely different network, such as your phone's hotspot.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Deliberately imports only app.config, which has no third-party dependencies,
# so this runs even from a plain Python that never had the project installed.
from app.config import (  # noqa: E402
    APP_HOST,
    APP_PORT,
    APP_TOKEN,
    LOOPBACK,
    MIN_TOKEN_LENGTH,
    WEAK_TOKENS,
    code_fingerprint,
)

LINE = "-" * 62
LOCAL = f"http://127.0.0.1:{APP_PORT}"

results: list[tuple[str, bool, str]] = []


def section(title: str) -> None:
    print(f"\n{title}\n{LINE}")


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


running_code_is_current: bool | None = None


def status_of(url: str, token: str | None, timeout: int = 10) -> tuple[int, str]:
    headers = {"X-Syslab-Token": token} if token else {}
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8")[:200]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")[:200]
    except (urllib.error.URLError, TimeoutError) as exc:
        return 0, str(exc)


def tailscale_binary() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    for guess in (
        r"C:\Program Files\Tailscale\tailscale.exe",
        r"C:\Program Files (x86)\Tailscale IPN\tailscale.exe",
        "/usr/bin/tailscale",
        "/usr/local/bin/tailscale",
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    ):
        if Path(guess).exists():
            return guess
    return None


def run(cmd: list[str], timeout: int = 20) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# --------------------------------------------------------------------------

def check_token() -> None:
    section("The token")
    if not APP_TOKEN or APP_TOKEN.strip().lower() in WEAK_TOKENS:
        record("APP_TOKEN is set", False, "missing or still a placeholder. Run: python scripts/new_token.py")
        return
    strong = len(APP_TOKEN.strip()) >= MIN_TOKEN_LENGTH
    record("APP_TOKEN is set", True, f"{len(APP_TOKEN.strip())} characters")
    record("APP_TOKEN is long enough", strong,
           "" if strong else f"under {MIN_TOKEN_LENGTH} characters. Run: python scripts/new_token.py")


def check_the_door() -> None:
    """The most important section in this file, so it fails loudly and stops."""
    global running_code_is_current
    section("The door, from this machine")

    code, body = status_of(f"{LOCAL}/api/health", token=None)
    if code == 0:
        record("App is answering", False, body)
        return
    record("App is answering", True, LOCAL)

    # Is the process running the code that is on disk? Asked here because a
    # stale process explains every other failure in this section at once.
    with_token = status_of(f"{LOCAL}/api/health", token=APP_TOKEN)
    payload = with_token[1] if with_token[0] == 200 else body
    try:
        running = json.loads(payload).get("code_fingerprint")
    except (json.JSONDecodeError, AttributeError):
        running = None
    running_code_is_current = running == code_fingerprint() if running else False

    if code == 200:
        # No token, and it answered anyway. Nothing else here matters.
        record("Refuses a request with no token", False, "HTTP 200: THE DOOR IS OPEN")
        print()
        print("  " + "=" * 58)
        print("  This app answered a request that carried no token at all.")
        print("  Anything that can reach this port can read and write your files.")
        print("  " + "=" * 58)
        if running is None:
            print("\n  Why: the running process predates the token check entirely. It is")
            print("  still the code from before Phase 06, because a running service is a")
            print("  snapshot of the code as it was when it started.")
        elif not running_code_is_current:
            print(f"\n  Why: the running process is older than the files on disk")
            print(f"  (running {running}, on disk {code_fingerprint()}).")
        else:
            print("\n  The running code matches the files on disk, which means this is a")
            print("  real bug rather than a stale process. Report this output.")

        print("\n  Fix it now, before anything else:")
        print("    powershell -ExecutionPolicy Bypass -File scripts\\service\\restart_windows.ps1")
        print("    .\\run.cmd scripts\\check_remote.py")
        print("\n  If you have already run that and this number has not changed, an old")
        print("  python is still holding the port and the new one could not start. See")
        print("  who owns it with:")
        print(f"    Get-NetTCPConnection -LocalPort {APP_PORT} -State Listen | Select OwningProcess")
        print("\n  APP_HOST is already 0.0.0.0 in .env, so the moment it restarts it will")
        print("  listen on your tailnet. It must be asking for a token by then.\n")
        record("Refuses a wrong token", False, "not tested: the door is open")
        record("Accepts the right token", False, "not tested: it accepts everything")
        return

    record("Refuses a request with no token", code in (401, 503),
           f"HTTP {code}" + (" (APP_TOKEN unset)" if code == 503 else ""))
    record("Running app matches the code on disk", bool(running_code_is_current),
           "" if running_code_is_current else "restart it: scripts\\service\\restart_windows.ps1")

    code, _ = status_of(f"{LOCAL}/api/health", token="definitely-not-the-token")
    record("Refuses a wrong token", code == 401, f"HTTP {code}")

    record("Accepts the right token", with_token[0] == 200, f"HTTP {with_token[0]}")
    if with_token[0] == 401:
        print("\n  The running app is using a different token to the one in .env.")
        print("  Restart it:  powershell -ExecutionPolicy Bypass -File scripts\\service\\restart_windows.ps1")

    code, _ = status_of(f"{LOCAL}/api/files/../../.env", token=APP_TOKEN)
    record("Still refuses to serve files outside the data folder", code in (400, 404), f"HTTP {code}")


def check_binding() -> None:
    section("What it is listening on")
    if APP_HOST in LOOPBACK:
        record("APP_HOST allows your tailnet in", False,
               f"APP_HOST={APP_HOST} means this machine only. Set APP_HOST=0.0.0.0 in .env, then restart.")
    else:
        record("APP_HOST allows your tailnet in", True, f"APP_HOST={APP_HOST}")


def check_tailscale() -> tuple[str | None, str | None]:
    section("Tailscale")
    binary = tailscale_binary()
    if not binary:
        record("Tailscale is installed", False, "not found. Install it from tailscale.com/download")
        return None, None
    record("Tailscale is installed", True, binary)

    raw = run([binary, "status", "--json"])
    if not raw.strip():
        record("Tailscale is running and signed in", False,
               "no status. Open the Tailscale app and sign in.")
        return None, None
    try:
        status = json.loads(raw)
    except json.JSONDecodeError:
        record("Tailscale is running and signed in", False, "could not read its status")
        return None, None

    state = status.get("BackendState", "")
    record("Tailscale is running and signed in", state == "Running", f"state: {state or 'unknown'}")

    self_node = status.get("Self") or {}
    ips = [ip for ip in (self_node.get("TailscaleIPs") or []) if ":" not in ip]
    name = (self_node.get("DNSName") or "").rstrip(".")
    record("This machine has a tailnet address", bool(ips), ", ".join(ips) or "none")
    if name:
        print(f"        MagicDNS name: {name}")

    peers = [p for p in (status.get("Peer") or {}).values() if p.get("Online")]
    if peers:
        print(f"        Other devices online: {', '.join(p.get('HostName', '?') for p in peers[:6])}")
    else:
        print("        No other devices are online yet. Install Tailscale on your laptop")
        print("        and sign in with the SAME account.")
    return (ips[0] if ips else None), (name or None)


def check_reachable(ip: str | None) -> None:
    section("Reachable on the tailnet address")
    if not ip:
        record("App answers on the tailnet address", False, "no tailnet address to try")
        return
    url = f"http://{ip}:{APP_PORT}/api/health"
    code, body = status_of(url, token=APP_TOKEN, timeout=8)
    if code == 200:
        record("App answers on the tailnet address", True, url)
    elif code == 0:
        record("App answers on the tailnet address", False, f"{url}: {body}")
        print("\n  Usual causes, in order of likelihood:")
        if running_code_is_current is False:
            print("    0. The running app has not been restarted since you changed .env,")
            print("       so it is still bound to whatever APP_HOST said when it started.")
            print("       Run: powershell -ExecutionPolicy Bypass -File scripts\\service\\restart_windows.ps1")
        print("    1. APP_HOST is still 127.0.0.1. Set it to 0.0.0.0 and restart the app.")
        print("    2. Windows Firewall is blocking the port. Allow it with, in an")
        print("       Administrator PowerShell:")
        print(f'         New-NetFirewallRule -DisplayName "syslab-server" -Direction Inbound '
              f"-Protocol TCP -LocalPort {APP_PORT} -Action Allow")
    else:
        record("App answers on the tailnet address", False, f"HTTP {code}")


def main() -> int:
    print("\nsyslab-server / Phase 06 remote access check")
    print(f"Machine: {platform.system()} {platform.release()}")

    check_token()
    check_the_door()
    check_binding()
    ip, name = check_tailscale()
    check_reachable(ip)

    section("Gate")
    passed = sum(1 for _, ok, _ in results if ok)
    for check, ok, _ in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {check}")
    print(f"\n  {passed} of {len(results)} checks passed")

    if passed != len(results):
        print("\n  Not ready yet. Paste this output back into the chat.\n")
        return 1

    section("Open this on your laptop")
    if name:
        print(f"    http://{name}:{APP_PORT}")
        print(f"    http://{ip}:{APP_PORT}      (if the name does not resolve)")
    else:
        print(f"    http://{ip}:{APP_PORT}")
    print("\n  Sign in with the token from .env. The browser remembers it for 30 days.")
    print("\n  The gate is not this machine. It is your laptop, on your phone's hotspot,")
    print("  off your home network entirely. If it works from there, Phase 06 passes.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
