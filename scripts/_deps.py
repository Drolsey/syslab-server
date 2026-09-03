"""Shared by the check scripts that genuinely need the project's packages.

Running `py scripts\\check_tools.py` instead of `.venv\\Scripts\\python` gives a
ModuleNotFoundError traceback that says nothing useful about the actual mistake.
This turns that into one sentence naming the interpreter you used and the one
you meant.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def require(*modules: str) -> None:
    missing = [m for m in modules if importlib.util.find_spec(m) is None]
    if not missing:
        return

    if sys.platform == "win32":
        venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
        activate = r".venv\Scripts\activate"
    else:
        venv_python = ROOT / ".venv" / "bin" / "python"
        activate = "source .venv/bin/activate"

    script = Path(sys.argv[0]).name
    print(f"\n  Missing: {', '.join(missing)}\n")
    print(f"  You ran this with:  {sys.executable}")

    if venv_python.exists():
        print(f"  It needs:           {venv_python}\n")
        if sys.platform == "win32":
            print("  Easiest fix, no PowerShell settings involved:")
            print(f"    run.cmd scripts\\{script}\n")
            print("  Or activate the virtualenv first, if that works on your machine:")
            print(f"    {activate}")
            print(f"    py scripts\\{script}\n")
        else:
            print("  Either activate the virtualenv first:")
            print(f"    {activate}")
            print(f"    py scripts/{script}\n")
            print("  Or point at it directly:")
            print(f"    {venv_python} scripts/{script}\n")
    else:
        print(f"  No virtualenv found at {ROOT / '.venv'}. Create one:\n")
        print("    py -m venv .venv")
        print(f"    {activate}")
        print("    pip install -r requirements.txt\n")
    raise SystemExit(1)
