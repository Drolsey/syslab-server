# Handover — syslab-server

Written 3 September 2026. A portable copy of the session notes, so a new chat
(or a different tool, or you in three weeks) can pick this up without a recap.

## Where it stands

Everything green. 174+ pytest tests pass. `scripts\check_database.py` passes
around 20 checks. The app runs as a Windows scheduled task (LogonType S4U)
and is reachable over Tailscale.

Working: file tools (list_files, read_pdf, read_excel, write_excel,
write_pdf), FTS5 document search, the async job lane, token auth and the web
UI, and four read-only PostgreSQL tools including `query_to_excel`.

Stack: qwen3:8b via Ollama at temperature 0, FastAPI, RTX 3060 12 GB.

## The client database

A client PostgreSQL instance on GCP; host, name and credentials live in
`.env`, which is not tracked. One real table with about 52,000 rows and 35
columns of asset-removal records. **Every one of those 35 column names needs
SQL quoting** — capitals and spaces throughout. Two `pg_stat_statements*`
entries also appear; those are PostgreSQL monitoring views, not client data.

## Run these to confirm the state

    py -m pytest -q
    py scripts\check_database.py
    powershell -ExecutionPolicy Bypass -File scripts\service\restart_windows.ps1

## Next, in order

1. **Verify the row count.** Run `SELECT count(*)` on the main table and
   confirm it matches the count `query_to_excel` wrote. The estimate from
   `list_tables` is the planner's estimate, not a count — the two measure
   different things and read thousands apart. A disagreement between count(*)
   and the export is a real bug; a disagreement with the estimate is not.
2. **Watch the phrase "files in the database".** The rule that resolves it
   landed after the last restart and has not been tested in a real session.
3. **Commit.** Nothing has been committed since 2 Sep. Delete
   `.git\index.lock` and `.git\index.lock.stale` first.
4. **Empty `data\_to_delete\`** — 41 decoy test files.

## Offered, not built

- **Structured field extraction** over the invoice PDFs. This is the real fix
  for numeric filtering ("labour more than 12000"), which today is a judgement
  the model makes rather than something exact by construction. Highest-value
  unbuilt thing.
- Caching query results, so "save that" refers to something concrete.
- The vLLM swap, for continuous batching and real multi-client concurrency.
- Trying qwen3:14b. We are near the edge of what an 8B holds.

## Four lessons that cost the most, as classes

1. **A tool must accept back exactly what it hands out.** `describe_table`
   handed the model a quoted table name and then rejected that same string.
   Likewise, anything a result *names* — a `query_as` field — will be echoed
   back as an *argument name*. Accept it rather than lecture.
2. **An ordinary psycopg cursor materialises the whole result during
   `execute()`.** A 200-row cap applied afterwards did nothing; the server
   still produced every row and timed out. Fixed with a server-side
   cursor — which moves the error from `execute()` to the first `FETCH`, so
   guard both as one unit.
3. **An example in a tool description is indistinguishable from an
   argument.** "whichever file mentions Meridian" was an illustration; the
   model searched for Meridian unprompted.
4. **An error that dumps 18 filenames drowns the context.** After two such
   errors the model said it had no database access while holding four
   database tools. It had not lost the tools; it had lost sight of them.

Fuller notes live in the project memory files, `feedback_agent_hardening.md`
and `project_database_tools.md` chief among them.
