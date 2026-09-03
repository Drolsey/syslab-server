"""Generate a strong APP_TOKEN and write it into .env.

    py scripts/new_token.py

Creates .env from .env.example if it does not exist, replaces the APP_TOKEN
line, and leaves everything else alone. Run it again any time you want to
revoke access from every device at once: change the token, restart the app,
and every signed-in browser is signed out.
"""

from __future__ import annotations

import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"


def main() -> int:
    token = secrets.token_urlsafe(32)

    if not ENV.exists():
        if not EXAMPLE.exists():
            print(f"  Neither {ENV.name} nor {EXAMPLE.name} exists. Nothing to edit.")
            return 1
        ENV.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"  Created .env from .env.example")

    text = ENV.read_text(encoding="utf-8")
    replaced, count = re.subn(r"(?m)^APP_TOKEN=.*$", f"APP_TOKEN={token}", text)
    if count == 0:
        replaced = text.rstrip() + f"\n\nAPP_TOKEN={token}\n"
        print("  No APP_TOKEN line found, appended one")
    ENV.write_text(replaced, encoding="utf-8")

    print(f"\n  New token written to {ENV}\n")
    print(f"    {token}\n")
    print("  Copy it somewhere you can reach from your laptop: a password manager,")
    print("  or a note on your phone. You will paste it into the browser once per")
    print("  device, and the browser remembers it for thirty days.\n")
    print("  It only takes effect when the app restarts:")
    print("    powershell -ExecutionPolicy Bypass -File scripts\\service\\restart_windows.ps1\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
