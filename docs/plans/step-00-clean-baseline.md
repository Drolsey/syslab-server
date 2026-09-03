# Step 0: Clean Baseline

Status: COMPLETE except the push and the service restart, both of which need Amro's machine.
Written 3 September 2026. Revised same day after Amro granted the assistant permission to run
commands and to fix bugs it finds without being re-prompted.

---

## 1. Why this step exists

Fifteen files in `app/` and `tests/` have been changed since commit `fe646dc` and never
committed, never gated, and never loaded by the running service. The service is therefore
executing older code than the code on disk.

Every later stage (tenancy, ingestion, vision, extraction, voice, vLLM) assumes a known-good
starting point. You cannot tell whether a future change broke something if you did not know
the system was working before you started.

**This step adds no features and changes no behaviour.** Its only product is confidence.

---

## 2. Who runs what, and why it is split

This is the most important section in the file. Getting it wrong produces a false
"the gates passed" when they passed somewhere that does not matter.

### The assistant has a Linux VM with the repo mounted

`device_bash` reaches a sandboxed Linux VM with `D:\GitHub\syslab-server` mounted live.
Same files, different machine. It has Python 3.10.12, network access to PyPI, a venv at
`~/.venv-syslab` built from `requirements.txt`, and file deletion permission in the repo.

**The assistant can run:** `pytest`, `check_tools.py`, `check_search.py`, `check_jobs.py`,
`git` (except `push`), file edits, and anything else that is pure Python plus the repo.

### Only Amro can run the Windows half

| Cannot be run from the VM | Why |
|---|---|
| `restart_windows.ps1`, `control_windows.ps1`, `check_services.py` | Task Scheduler and PowerShell exist only on Windows |
| `check_ollama.py`, `check_agent.py`, `check_endtoend.py`, `bench_models.py` | Need Ollama and the RTX 3060 |
| `check_database.py` against the real instance | The VM has no route to `34.77.31.30:5432`. Confirmed: `Network is unreachable` |
| `check_remote.py` | Needs the tailnet |
| `git push` | Credentials live in Windows Credential Manager |

### A VM pass is evidence, not proof

The VM is Linux on Python 3.10.12. Amro's machine is Windows 11 on Python 3.11.9. This
project has already been bitten by Windows-specific path handling more than once
(`config.resolve_in_data_dir` normalising backslashes, `config._path` resolving against the
working directory). A green run in the VM says the logic is sound. It does not say the
product works on the machine that serves customers.

**So the four gates must still be run once on Windows before the commit is trusted.** That
run is four pasted commands and about a minute of his time. It is the cheapest insurance in
the whole project.

---

## 3. Definition of done

1. `.git/index.lock` gone and git commands working. **DONE**
2. Gates run and recorded in the VM. **DONE, see section 5**
3. Same four gates run once on Windows by Amro. **DONE, all four green**
4. Service restarted and its reported code fingerprint matching disk. **OUTSTANDING, Windows only**

The two outstanding items are `git push` and the service restart. Both need Amro's machine.
5. `public."Report"` row count checked. **DONE. 52,190 by count(*), and the export matches exactly.**
6. The 15 modified files committed and pushed. **Committed as `23ab5ea`. PUSH OUTSTANDING.**
7. `HANDOVER.md` and project memory updated. **DONE**

---

## 4. Verified state of the repo

| Fact | Value |
|---|---|
| Repo | `D:\GitHub\syslab-server`, branch `master` |
| HEAD | `fe646dc` "Add document search and export, and harden the database path" |
| Remote | `https://github.com/Drolsey/syslab-server.git`, local level with origin (0 ahead, 0 behind) |
| Uncommitted | 15 modified files, 380 insertions, 37 deletions. No untracked files. |
| OS | Windows 11 build 10.0.26200, PowerShell in VS Code |
| Python on Windows | `py` works directly. `run.cmd` is a fallback only, kept in case the execution-policy block on `.venv\Scripts\activate` returns. |
| Expected test count | 201 |

The 15 files: `.env.example`, `app/{agent,config,db,jobs,main,search,tools}.py`,
`app/web/index.html`, `tests/test_{agent,api,db,jobs,search,tools}.py`.

What they contain is in project memory as `feedback_latent_bugs.md`: eight latent bugs found
by reading the code, each with a regression test. **None changes how the model reasons.**
That is why the pass is safe to commit as one unit.

---

## 5. Progress log

### 3 September, assistant, in the Linux VM

**Git lock cleared.** `.git/index.lock` (0 bytes, 07:34) removed. Deletion in this repo was
granted for the session, so this no longer needs Amro.

**Environment built.** venv at `~/.venv-syslab` from `requirements.txt`, all imports verified:
fastapi, openpyxl, fitz, reportlab, psycopg, httpx, pytest.

**Gate results:**

| Gate | Result | Notes |
|---|---|---|
| `pytest -q` | **201 passed**, 1 unrelated deprecation warning, 26.4s | Matches the expected count exactly |
| `check_tools.py` | **12 of 12 passed**, exit 0 | Both path-escape refusals held; missing-file error still names the closest filenames |
| `check_search.py` | **9 of 9 passed**, exit 0 | Index rebuilt 60 documents in 6.23s; it also cleaned up all 41 of its own test files this time |
| `check_database.py` | **FAIL at connection**, exit 1 | `Network is unreachable` to `34.77.31.30:5432`. The VM has no egress to that host. **This is not a code failure and it says nothing about Windows.** |

**Exit codes verified** individually rather than through a pipe: `check_database` returns 1
on failure, the other two return 0 on pass. Automation can trust them.

Run with `--basetemp` outside the mount, because pytest's `tmp_path` cleanup throws
`RecursionError` inside the mounted folder.

**No code bugs found.** Nothing in `app/` needed changing.

### 3 September, Amro, on Windows 11

| Gate | Result | Notes |
|---|---|---|
| `pytest -q` | **201 passed**, 24.9s | Same count as the VM. One unrelated reportlab deprecation warning. |
| `check_tools.py` | **12 of 12 passed** | Identical to the VM run, including both path-escape refusals |
| `check_search.py` | **9 of 9 passed** | Rebuilt 19 files in 4.91s, indexed 60 in 4.61s, cleaned up all 41 test files |
| `check_database.py` | **22 of 22 passed** | The allowlist is open. See below. |

**The database is reachable from Windows.** This overturns the standing note that the
database tools were blocked on the client's IP allowlist. Confirmed working: TLSv1.3, a
read-only session, a server-refused `CREATE TABLE`, SELECT granted on one table and nothing
else, a 15s statement timeout, `query_to_excel` writing 600 rows past the 200-row model cap,
and an unfiltered query on the biggest table returning 200 rows in 1.9s.

**Two observations from his output that are worth recording:**

1. **The index was stale in both directions.** It opened holding 33 documents for a folder of
   19 files, with 2 files not indexed at all. That is 14 ghost entries plus 2 missing ones,
   which is precisely the Class 2 bug the uncommitted fix addresses. The running service is
   still on the old code, so this is confirming evidence that the fix is needed rather than a
   new problem. The rebuild corrected it.

2. **`list_tables` reports `public."Report"` at roughly 49,795 rows**, against the 52,181 the
   export is believed to hold. That figure is `pg_class.reltuples`, a planner estimate
   refreshed by ANALYZE, so it is allowed to drift. A 4.6 percent gap is well inside normal
   staleness. It still has to be confirmed rather than assumed, which is Task C.

### Assistant error, 3 September, recorded because it cost a round trip

The first version of `scripts/_rowcount_check.py` did `result["rows"][0][0]` and died with
`KeyError: 0`. `db.run_sql` returns rows as **dicts keyed by unique column label**, not as bare
lists, which is a deliberate design choice documented in a long comment in `app/db.py` and
recorded in `feedback_latent_bugs.md` as one of the eight fixes. I assumed the shape instead of
reading it.

Rewritten to pull the single value out whichever shape comes back, and unit-tested against both
before handing it over. The docstring also raised a `SyntaxWarning` for `\_` and is now a raw
string.

The lesson generalises and belongs with the others: **read the return shape, do not infer it
from the name.** A helper that reports on someone else's data is exactly where an assumed shape
survives review and fails in front of the user.

### Row count resolved, 3 September

| Number | Value |
|---|---|
| `count(*)` from the server | **52,190** |
| Planner estimate (`reltuples`) | 49,795, drift +2,395 (+4.6%) |
| Data rows in `Report_export_2026-09-02.xlsx` | **52,190** |

The two authoritative numbers agree exactly, so `query_to_excel` is faithful and the export
path has no defect.

**Why the recorded 52,181 did not match, and why it is not a bug.** The file is named
`Report_export_2026-09-02.xlsx` but its modification time is 3 September 06:32 UTC. It was
regenerated a day after the date in its own filename. 52,181 was the true count on 2 September;
the table has since gained 9 rows, and the newer export captured them. Both numbers were right
at their own moments.

Worth knowing rather than fixing: **a date in a filename is not evidence of when the file was
written.** For a tool a customer will use to produce records, that is a small trap. If exports
should be dated reliably, the date belongs in a cell inside the workbook, written by the tool,
not in a name a caller chooses.

---

## 6. Remaining tasks

### Task A: Amro runs the four gates on Windows

```powershell
cd D:\GitHub\syslab-server
py -m pytest -q
py scripts\check_tools.py
py scripts\check_search.py
py scripts\check_database.py
```

Expected, based on the VM run: 201 passed, 12 of 12, 9 of 9. `check_database` is the one
genuinely unknown result, because Windows may or may not be inside the client's IP allowlist.

Paste anything that differs. A difference between the two machines is a real finding and
almost certainly a Windows path or encoding issue.

### Task B: Restart the service and prove it loaded the new code

```powershell
powershell -ExecutionPolicy Bypass -File scripts\service\restart_windows.ps1
py scripts\check_services.py
```

The restart is not the proof. `check_services.py` compares the code fingerprint reported by
`/api/health` against the files on disk, and **that comparison is the proof.** A stale
process has caused three separate confusing sessions in this project.

If the port is still held by an orphaned python after the restart, see
`feedback_windows_services.md`. `Stop-ScheduledTask` alone does not kill it.

Do not pass `-IncludeOllama` unless Ollama itself needs restarting. It evicts qwen3:8b from
VRAM and the next request pays a cold load.

### Task C: Verify the row count

Now unblocked, because the database answers from Windows.

Do not ask the running web UI. It is still executing pre-fix code, and `run_sql` there caps
rows, so an answer from it would be confounded. Use the throwaway script instead, which
compares the three numbers that should agree:

```powershell
py scripts\_rowcount_check.py
```

It prints `count(*)` from the server (the only authoritative number), the planner's estimate
for comparison, and the data rows actually present in `Report_export_2026-09-02.xlsx`.

**Pass:** `count(*)` and the export agree. A gap between `count(*)` and the estimate is normal
and not a finding.

**Fail:** `count(*)` and the export disagree. That is a real defect in the export path and gets
diagnosed before the commit, since `query_to_excel` is a shipped tool a customer would rely on.

Delete the script once the answer is recorded: `del scripts\_rowcount_check.py`

### Task D: Commit and push

The assistant can stage and commit from the VM. The push is Amro's, because the credentials
are in Windows Credential Manager.

```
git add -A
git status        # must list exactly the 15 files and nothing else
git commit -F docs/plans/step-00-commit-message.txt
```

then, on Windows:

```powershell
git push
```

Draft commit message:

```
Fix eight latent bugs found by a full code audit

Found by reading app/ rather than from a failing transcript. Each has a
regression test. Test count 190 -> 201. None of these change how the model
reasons; all of them change what the code actually does.

- read_excel counted openpyxl trailing blank rows as real rows, so a fully
  read sheet reported truncated=true
- write_excel derived started_at_row from ws.max_row before appending, so
  every new file claimed its data began on row 2
- run_sql keyed rows by column name, so duplicate column names silently
  overwrote each other
- the search index was written only by the upload endpoint, so tool-written
  files were never indexed and deleted files were never forgotten
- db.check_statement scanned string literals, refusing ordinary English
  containing DO, SET or COMMENT
- /api/upload read the whole file into memory before applying the size limit
- jobs.Lane.submit counted only QUEUED against max_queued, making the limit
  depend on thread timing
- agent.py sliced json.dumps() mid-key and handed the model invalid JSON

Gates: pytest 201 passed, check_tools 12/12, check_search 9/9 on Linux and
on Windows. check_database and check_services run on Windows only.
```

### Task E: Leave the record accurate

1. `HANDOVER.md` in the repo root: state that the bug-fix pass is committed and gated, and
   what the gates actually returned.
2. Project memory `handover_next_session.md`: replace "DO THIS NEXT" with the Step 1 work.
3. Record the baseline: test count, the `/api/health` fingerprint after the restart, and the
   fact that `bench_results.json` holds the qwen3:8b figures. Everything later is measured
   against these.

---

## 7. Rules for the assistant

Revised 3 September at Amro's instruction.

1. **Run the commands.** Do not hand Amro a command list and wait, and do not narrate before
   acting. Run it, read the output, report the result.
2. **Fix a bug the moment you find it.** No re-prompting. Write the regression test in the
   same pass, re-run the gate, and say plainly what changed and why.
3. **The one carve-out: `app/agent.py` behaviour.** Latent code bugs in that file are ordinary
   bugs, fix them. Changes to how the model is prompted, guarded, or told about errors are
   NOT. Those go one at a time with a real session between each, because four shipped at once
   on 3 September made the assistant measurably worse and all four were reverted. See
   `feedback_agent_hardening.md`.
4. **Never loosen a gate to make it pass.** A failing gate is the gate earning its keep.
5. **Three outcomes, never two:** passed, failed, and not tested. Anything the VM cannot reach
   is "not tested", never a pass and never a code failure.
6. **Say where a result came from.** "201 passed in the Linux VM" and "201 passed on Windows"
   are different claims. Do not let one stand in for the other.
7. **Report every file you change**, including scripts and docs.
8. **Do not start Step 1.** The ownership boundary is a separate conversation.

---

## 8. Findings raised, not yet actioned

**Gate scripts print stale next-step instructions on success.** `check_env`, `check_ollama`,
`check_tools`, `check_agent` and `check_endtoend` all end with some form of "Paste this output
back into the chat and we move to Phase 0X". Every phase is complete, so the instruction is
wrong, and now that the assistant runs the gates itself the "paste it back" half is wrong too.

This is exactly the defect class recorded in `feedback_gate_scripts.md`, where
`check_services.py` told Amro to reboot after he had already rebooted and he wrote "I dont
understand what to do with rebooting."

It is cosmetic, it touches five files, and it changes no behaviour. Recommendation: fix it as
its own small commit **immediately after** the Step 0 commit lands, so the bug-fix commit stays
one clean, well-scoped unit. Not folded in.

---

## 9. Rollback

Before the commit, the whole pass can be abandoned with `git stash`, which returns the tree to
`fe646dc` and keeps the changes retrievable via `git stash pop`.

After the commit, use `git revert <sha>` rather than resetting, since the commit will be on
`origin/master`.

---

## 10. What comes after

**Step 1, the ownership boundary.** Tenant identity threaded through storage, the search index,
the job lane and database credentials, before any customer data exists. It is the single most
expensive thing to retrofit, which is why it precedes vision, extraction and voice.

**Outside the chain: the vLLM provider seam.** Make `app/llm.py` speak the OpenAI-compatible
dialect and keep every Ollama-ism behind it, add a provider contract test and a golden question
set. Roughly an hour, doable at any point, and it turns the eventual Ollama-to-vLLM swap into a
base URL and a model name rather than a rewrite.
