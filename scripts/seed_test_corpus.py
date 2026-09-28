"""Make the test corpus its own company, with its folders kept (Step 11.2).

    py scripts\\seed_test_corpus.py

Creates the tenant `syslab-test-corpus` if it is not there, copies
tests/fixtures/corpus/contracts/ and formats/ into data/syslab-test-corpus/
with the folder structure intact, then ingests and indexes it. Safe to run
again: files are copied with their timestamps, so an unchanged file is not
re-produced.

Only documents are copied. reference_text/ and the golden, manifest and
baseline JSON are ground truth for scripts/check_retrieval.py, not corpus: a
reference transcript sitting beside its contract would answer every question
twice. The fixtures stay where they are, in git, for the tests.

formats/format_corrupt.pdf fails ingestion ON PURPOSE. It is there so a
failed document is always on hand to look at.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import context, ingest, intake, tenancy  # noqa: E402
from app.config import data_dir, doc_id  # noqa: E402

TENANT = "syslab-test-corpus"
NAME = "Syslab test corpus"
CORPUS = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "corpus"
FOLDERS = ("contracts", "formats")
NOT_CORPUS = ("sentinels.json",)


def main() -> int:
    connection = tenancy.connect()
    try:
        if tenancy.get_tenant(TENANT, connection=connection) is None:
            tenancy.create_tenant(NAME, tenant_id=TENANT, connection=connection)
            print(f"  created tenant {TENANT!r}")
        else:
            print(f"  tenant {TENANT!r} already exists")
    finally:
        connection.close()

    with context.use_tenant(TENANT):
        target = data_dir()
        for folder in FOLDERS:
            shutil.copytree(CORPUS / folder, target / folder, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(*NOT_CORPUS))
        print(f"  copied {', '.join(FOLDERS)} into {target}")

        print("  ingesting and indexing ...")
        intake.run_folder(lambda *_: None)

        counts = {"ready": 0, "outstanding": 0, "failed": []}
        for path in sorted(target.rglob("*")):
            if not path.is_file() or not ingest.producers_for(path.suffix):
                continue
            state = ingest.status(doc_id(path))
            if state["failed"]:
                counts["failed"].append(state["name"])
            elif state["ready"]:
                counts["ready"] += 1
            else:
                counts["outstanding"] += 1

    print(f"  ready {counts['ready']}, outstanding {counts['outstanding']}, "
          f"failed {len(counts['failed'])}")
    for name in counts["failed"]:
        print(f"    failed: {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
