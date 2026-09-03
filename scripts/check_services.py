"""Phase 05 gate: is this thing actually running on its own?

Run it after installing the tasks, and again after a reboot. The second run is
the one that counts: it proves the assistant came back without you starting it.

    py scripts/check_services.py
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import APP_PORT, APP_TOKEN, OLLAMA_HOST, OLLAMA_MODEL, code_fingerprint  # noqa: E402

LINE = "-" * 62
APP_URL = f"http://127.0.0.1:{APP_PORT}"
TASKS = ["syslab-ollama", "syslab-server"]

# The token this check sends. Normally config.APP_TOKEN, but see
# _recover_from_401: an environment variable can shadow .env, and when it does
# we fall back to the value written in the file so the rest of the checks can
# still run instead of being abandoned.
_active_token: str = APP_TOKEN or ""

results: list[tuple[str, bool, str]] = []

# Findings that are real and worth shouting about, but are NOT this gate's
# subject. Phase 05 asks one question: does the service come back on its own?
# A stale variable in the shell you happen to be typing in does not change the
# answer, and counting it as a gate failure told Amro he had not passed when he
# had. Warnings print loudly and are excluded from the verdict.
warnings: list[tuple[str, str]] = []
app_started_at: datetime | None = None
app_answered = False
app_code_is_current: bool | None = None


def boot_time() -> datetime | None:
    """When did this machine last start? Used to tell an automatic start from a
    manual one, so this script can say whether you are done rather than telling
    you to reboot forever."""
    if platform.system() != "Windows":
        try:
            with open("/proc/uptime", encoding="utf-8") as handle:
                return datetime.now() - timedelta(seconds=float(handle.read().split()[0]))
        except OSError:
            return None
    try:
        milliseconds = ctypes.windll.kernel32.GetTickCount64()
        return datetime.now() - timedelta(milliseconds=milliseconds)
    except Exception:  # noqa: BLE001
        return None


def task_last_run(name: str) -> datetime | None:
    """When the scheduled task itself last started, straight from Task Scheduler."""
    if platform.system() != "Windows":
        return None
    output = run([
        "powershell", "-NoProfile", "-Command",
        f"(Get-ScheduledTaskInfo -TaskName '{name}').LastRunTime.ToString('o')",
    ]).strip()
    try:
        return datetime.fromisoformat(output.split("+")[0].split("Z")[0])
    except ValueError:
        return None


def section(title: str) -> None:
    print(f"\n{title}\n{LINE}")


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  ' + detail if detail else ''}")


def warn(name: str, detail: str) -> None:
    """A real finding that is not about the thing this gate measures."""
    warnings.append((name, detail))
    print(f"  WARN  {name}{'  ' + detail if detail else ''}")


def get(url: str, timeout: int = 10):
    # From Phase 06 the API needs the token. Scripts read it from .env.
    request = urllib.request.Request(url, headers={"X-Syslab-Token": _active_token})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# --------------------------------------------------------------------------
# Why did the app refuse our token? Three causes, and they need three answers.
# --------------------------------------------------------------------------

def _token_in_env_file() -> str | None:
    """APP_TOKEN as it is literally written in .env.

    Parsed by hand rather than through dotenv, because a diagnostic must not
    depend on the thing it is diagnosing. The point of reading it separately:
    load_dotenv does NOT override a variable that is already in the
    environment, so config.APP_TOKEN and this value can legitimately differ.
    That difference is invisible from the outside and is the single most
    confusing way this check can fail.
    """
    path = Path(__file__).resolve().parent.parent / ".env"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != "APP_TOKEN":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value
    return None


def _recover_from_401():
    """Name the actual cause of a 401, and recover from it where possible.

    Returns the health payload if a retry with the token from .env worked,
    otherwise None. Never raises: a crash here would take out every check
    below it, and this function only exists to explain a failure.
    """
    global _active_token
    try:
        file_token = _token_in_env_file()

        if file_token is None:
            print("\n  Cause: .env has no APP_TOKEN line, or could not be read, so")
            print("  this check had nothing to send.")
            print("  Fix:   py scripts\\new_token.py")
            return None

        if file_token == (APP_TOKEN or ""):
            print("\n  Cause: the token this check sent IS the one in .env, and the app")
            print("  refused it anyway. So the app is holding an OLDER token, from")
            print("  before .env was last changed. A running service is a snapshot of")
            print("  its configuration as well as of its code.")
            print("  Fix:   powershell -ExecutionPolicy Bypass -File "
                  "scripts\\service\\restart_windows.ps1")
            return None

        # The two differ. Nothing but a pre-set environment variable can cause
        # that, because load_dotenv would otherwise have supplied the file's
        # value. Restarting the app would not have helped, and the old message
        # here told you to do exactly that.
        print("\n  Cause: the token this check sent is NOT the one in .env.")
        print("  APP_TOKEN is set as an environment variable, and load_dotenv does")
        print("  not override a variable that is already set, so .env was ignored")
        print("  by THIS PROCESS. The app, started by Task Scheduler, has its own")
        print("  environment and read the file normally.")
        print("  This is a problem with the shell you are in, not with the app.")

        _active_token = file_token
        try:
            health = get(f"{APP_URL}/api/health")
        except Exception as exc:  # noqa: BLE001
            _active_token = APP_TOKEN or ""
            print(f"  Retrying with the token from .env failed too ({exc}), so the")
            print("  app is holding a third value. Restart it and run this again.")
            return None

        print("  Retrying with the token from .env worked, so THE APP IS FINE and")
        print("  this check was the thing that was wrong. Carrying on with the")
        print("  file's token so the rest of the checks below are real.")
        print("  Clear it in this shell:   Remove-Item Env:APP_TOKEN")
        print("  If it returns in a new shell it was set permanently. Check with:")
        print("    [Environment]::GetEnvironmentVariable('APP_TOKEN','User')")
        return health
    except Exception as exc:  # noqa: BLE001
        print(f"\n  (could not work out why the token was refused: "
              f"{type(exc).__name__}: {exc})")
        return None


# --------------------------------------------------------------------------

def check_ollama() -> None:
    section("Ollama")
    try:
        tags = get(f"{OLLAMA_HOST}/api/tags")
    except (urllib.error.URLError, TimeoutError) as exc:
        record("Ollama is answering", False, f"{OLLAMA_HOST}: {exc}")
        return
    names = [m.get("name", "") for m in tags.get("models", [])]
    record("Ollama is answering", True, OLLAMA_HOST)
    record(
        f"{OLLAMA_MODEL} is present to this process",
        OLLAMA_MODEL in names,
        f"models visible: {', '.join(names) or '(none)'}",
    )


def check_app() -> None:
    section("Web app")
    try:
        health = get(f"{APP_URL}/api/health")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            health = _recover_from_401()
            if health is None:
                record("App is answering", False,
                       "it is up but refused the token this check sent; the cause is "
                       "named above")
                return
            warn("Token this check sent",
                 "an environment variable shadowed .env; the app itself accepted "
                 "the file's token, so this is your shell, not the service")
        elif exc.code == 503:
            record("App is answering", False,
                   "it is up but APP_TOKEN is not set. Run: py scripts\\new_token.py")
            return
        else:
            record("App is answering", False, f"{APP_URL}: HTTP {exc.code}")
            return
    except (urllib.error.URLError, TimeoutError) as exc:
        record("App is answering", False, f"{APP_URL}: {exc}")
        print("\n  If Ollama is up but this is not, read logs\\server.log. The usual")
        print("  cause is the virtualenv: a venv built on the Microsoft Store Python")
        print("  can fail under a scheduled task.")
        return
    global app_started_at, app_answered, app_code_is_current
    app_answered = True
    record("App is answering", True, APP_URL)
    record("App reports its model", bool(health.get("model")), str(health.get("model")))
    if health.get("started_at"):
        try:
            app_started_at = datetime.fromisoformat(health["started_at"])
        except ValueError:
            app_started_at = None

    # A running service is a snapshot of the code as it was when it started.
    running = health.get("code_fingerprint")
    on_disk = code_fingerprint()
    if running is None:
        app_code_is_current = False
        record(
            "Running app matches the code on disk",
            False,
            "the running app is too old to report its version, so it predates your latest edits",
        )
    else:
        app_code_is_current = running == on_disk
        record(
            "Running app matches the code on disk",
            app_code_is_current,
            f"running {running}, on disk {on_disk}" if not app_code_is_current else running,
        )
    try:
        listing = get(f"{APP_URL}/api/files")
        record("App can see its data folder", True, f"{listing['count']} file(s)")
    except Exception as exc:  # noqa: BLE001
        record("App can see its data folder", False, repr(exc))


def check_windows_tasks() -> None:
    section("Scheduled tasks")
    for name in TASKS:
        output = run(["schtasks", "/query", "/tn", name, "/fo", "list"])
        if not output.strip():
            record(f"{name} is registered", False, "not found")
            continue
        state = ""
        for line in output.splitlines():
            if line.lower().startswith("status"):
                state = line.split(":", 1)[1].strip()
        record(f"{name} is registered", True, f"status: {state or 'unknown'}")
        if state.lower() == "disabled":
            record(f"{name} is enabled", False, "the task exists but is disabled")


def check_windows_power() -> None:
    section("Power")
    output = run(["powercfg", "/query", "SCHEME_CURRENT", "SUB_SLEEP", "STANDBYIDLE"])
    value = None
    for line in output.splitlines():
        if "Current AC Power Setting Index" in line:
            value = line.split(":")[-1].strip()
    if value is None:
        record("Sleep on mains is off", False, "could not read the power setting")
        return
    asleep = int(value, 16) if value.startswith("0x") else int(value)
    record(
        "Sleep on mains is off",
        asleep == 0,
        "never sleeps" if asleep == 0 else f"sleeps after {asleep // 60} minutes, which will cut off your laptop",
    )


def check_linux_units() -> None:
    section("systemd")
    for unit in ("ollama", "syslab-server"):
        state = run(["systemctl", "is-active", unit]).strip()
        enabled = run(["systemctl", "is-enabled", unit]).strip()
        record(f"{unit} is active", state == "active", f"{state or 'unknown'}, {enabled or 'unknown'} at boot")


def main() -> int:
    print("\nsyslab-server / Phase 05 service check")
    build = platform.version()
    label = f"{platform.system()} {platform.release()} (build {build})"
    if platform.system() == "Windows":
        try:
            if int(build.split(".")[-1]) >= 22000 and platform.release() != "11":
                label += "  [build says Windows 11]"
        except (ValueError, IndexError):
            pass
    print(f"Machine: {label}")

    check_ollama()
    check_app()
    if platform.system() == "Windows":
        check_windows_tasks()
        check_windows_power()
    else:
        check_linux_units()

    section("Gate")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    for name, detail in warnings:
        print(f"  WARN  {name}")
    print(f"\n  {passed} of {len(results)} checks passed")
    if warnings:
        print(f"  {len(warnings)} warning(s), none of them about the service itself:")
        for name, detail in warnings:
            print(f"    - {name}: {detail}")

    # Worth answering even if something else failed: knowing whether it started
    # itself is the actual point of this phase.
    started_itself = report_boot_verdict()

    if passed != len(results):
        print("\n  Phase 05 does not pass yet. Paste this output back into the chat.\n")
        return 1
    if started_itself:
        print("\n  PHASE 05 PASSES. It came back on its own after the last restart.")
        print("  Nothing to reboot again, and nothing further to do here.\n")
    return 0


def report_boot_verdict() -> bool:
    """Say plainly whether the app started itself at boot. Returns True if so."""
    section("Did it start on its own?")

    booted = boot_time()
    if booted is None:
        print("  Could not read this machine's boot time, so I cannot tell you")
        print("  automatically. Judge it yourself: if you have not started anything")
        print("  since the last restart, Phase 05 passes.")
        return False

    uptime_minutes = (datetime.now() - booted).total_seconds() / 60
    print(f"  Machine booted:  {booted:%Y-%m-%d %H:%M:%S}   ({uptime_minutes:.0f} minutes ago)")

    # Task Scheduler is the authority here. The app's own start time is only a
    # fallback, and an app running older code cannot report one at all.
    from_task = task_last_run("syslab-server")
    started = from_task or app_started_at
    source = "Task Scheduler" if from_task else "the app itself"

    if started is None:
        if not app_answered:
            print("  The app is not answering, so there is nothing to judge yet.")
        elif app_code_is_current is False:
            print("  The app is answering, but it is running code from before this check")
            print("  existed, so it cannot say when it started, and Task Scheduler did not")
            print("  answer either. Restart the task and run this again:")
            print("     powershell -ExecutionPolicy Bypass -File scripts\\service\\restart_windows.ps1")
        else:
            print("  Could not read when the app started, from Task Scheduler or from the")
            print("  app itself. Judge it yourself: if you have not started anything since")
            print("  the last restart, Phase 05 passes.")
        return False

    delay = (started - booted).total_seconds()
    print(f"  App started:     {started:%Y-%m-%d %H:%M:%S}   (according to {source})")
    print(f"  Gap:             {delay:.0f} seconds after the machine came up")

    if uptime_minutes < 2:
        print("\n  The machine only just started. Give it another minute and run this")
        print("  again, so the boot-start has a fair chance to happen.")
        return False

    if -60 <= delay <= 300:
        print("\n  It started roughly when the machine did, so Task Scheduler started")
        print("  it, not you. That is exactly what this phase was for.")
        if app_code_is_current is False:
            print("\n  Note: it is running older code than what is in the folder now. That")
            print("  does not affect this phase, but restart the task before testing any")
            print("  change you have made since it started.")
        return True

    print(f"\n  It is running, but it started {delay / 60:.0f} minutes after the machine did.")
    print("  Either it restarted after a crash (check logs\\server.log), or you started")
    print("  it by hand. If you did not start it, this is fine. If you are not sure:")
    print("  restart the desktop, wait two minutes, run this once more, and this")
    print("  section will say it started on its own.")
    return False


if __name__ == "__main__":
    sys.exit(main())
