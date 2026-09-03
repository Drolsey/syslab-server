# Handover — syslab-server

Written 3 September 2026. A portable copy of the session notes, so a new chat
(or a different tool, or you in three weeks) can pick this up without a recap.

## Where it stands

Everything green, and gated on Windows on 3 September: pytest 201 passed,
`check_tools` 12 of 12, `check_search` 9 of 9, `check_database` 22 of 22
against the live client instance. The app runs as a Windows scheduled task
(LogonType S4U) and is reachable over Tailscale.

The eight-bug audit pass is committed as `23ab5ea`. The running service still
holds pre-fix code until it is restarted.

Working: file tools (list_files, read_pdf, read_excel, write_excel,
write_pdf), FTS5 document search, the async job lane, token auth and the web
UI, and four read-only PostgreSQL tools including `query_to_excel`.

Stack: qwen3:8b via Ollama at temperature 0, FastAPI, RTX 3060 12 GB.

## The client database

A client PostgreSQL instance on GCP; host, name and credentials live in
`.env`, which is not tracked. One real table, `public."Report"`, holding
**52,190 rows** by `count(*)` on 3 September, with 35 columns of
asset-removal records. `list_tables` shows roughly 49,795, which is
`pg_class.reltuples`, a planner estimate refreshed by ANALYZE. A 4.6 per cent
gap between the two is ordinary staleness and is not a finding. **Every one of those 35 column names needs
SQL quoting** — capitals and spaces throughout. Two `pg_stat_statements*`
entries also appear; those are PostgreSQL monitoring views, not client data.

## Run these to confirm the state

    py -m pytest -q
    py scripts\check_database.py
    powershell -ExecutionPolicy Bypass -File scripts\service\restart_windows.ps1

## Next, in order

1. **Restart the service** so it loads `23ab5ea`, then run
   `py scripts\check_services.py` and confirm the code fingerprint it reports
   matches the files on disk. The restart is not the proof; that comparison is.
2. **Push.** `23ab5ea` is committed locally and not yet on GitHub.
3. **Watch the phrase "files in the database".** The rule that resolves it
   landed after the last restart and has not been tested in a real session.
4. **Step 1, the ownership boundary.** See `docs/plans/`. Tenant identity
   threaded through storage, search, jobs and database credentials, before any
   customer data exists. It comes before vision, extraction and voice because
   it is the most expensive thing to retrofit.

Done and no longer on this list: the row count is verified at 52,190 and the
export matches it exactly, so `query_to_excel` is faithful; the git locks are
cleared; `data\_to_delete\` is empty.

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

5. **Read a return shape, do not infer it from the name.** `run_sql` returns
   rows as dicts keyed by unique column label, not as lists, and a helper
   written against the assumed shape died with `KeyError: 0`. The deliberate
   design is documented in `app/db.py`; the assumption was not checked against
   it.

Fuller notes live in the project memory files, `feedback_agent_hardening.md`
and `project_database_tools.md` chief among them.
