"""The tool-calling loop. This is the part that makes it an assistant.

The shape of it, in one paragraph: we send the model the conversation plus a
description of every tool it may use. It either answers, or it replies "call
read_excel with these arguments". If it asks for a tool, we run the real Python
function, append the result to the conversation as a tool message, and send the
whole thing back. It keeps going until it answers or hits MAX_TOOL_STEPS.

The model never touches a file. It only ever asks us to, and we decide whether
that is allowed.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import inspect
import json
from typing import Any, Callable

from app import db, jobs, llm, search, tools
from app.config import MAX_TOOL_STEPS

# --------------------------------------------------------------------------
# what the model is allowed to call
# --------------------------------------------------------------------------

def queue_image(prompt: str, filename: str | None = None) -> dict:
    """Start an image and return immediately. Never wait for it here."""
    job = jobs.lane.submit("generate_image", {"prompt": prompt, "filename": filename})
    return {
        "job_id": job.id,
        "status": job.status,
        "position_in_queue": jobs.lane.position_of(job.id),
        "note": "Started. Tell the user the job id and that it is running; do not "
                "wait for it. They can ask you to check on it.",
    }


def job_status(job_id: str) -> dict:
    """Look up one job the user asked about."""
    return jobs.lane.get(job_id).public(jobs.lane.position_of(job_id))


REGISTRY: dict[str, Callable[..., dict]] = {
    "list_files": tools.list_files,
    "search_files": search.search,
    "queue_image": queue_image,
    "job_status": job_status,
    "list_tables": db.list_tables,
    "describe_table": db.describe_table,
    "run_sql": db.run_sql,
    "query_to_excel": db.query_to_excel,
    "read_pdf": tools.read_pdf,
    "read_excel": tools.read_excel,
    "write_excel": tools.write_excel,
    "write_pdf": tools.write_pdf,
}

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List the files available in the user's data folder, with sizes, "
                "modification dates, and which tool reads each one (the read_with "
                "field). Call this first when the user refers to a file without "
                "giving its exact name, and use read_with to pick the right tool "
                "rather than guessing from the file name."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "queue_image",
            "description": (
                "Start generating an image. Returns a job id straight away and does NOT "
                "wait for the picture. Tell the user it has started and give them the id. "
                "Never call this repeatedly hoping for a result."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "What the image should show."},
                    "filename": {"type": "string", "description": "Optional .png name to save as."},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "job_status",
            "description": (
                "Check on a job the user started earlier, by its id. Use this when they "
                "ask whether something is ready."
            ),
            "parameters": {
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tables",
            "description": (
                "List the tables in the connected customer database, with approximate "
                "row counts. Call this FIRST for any question about the database. "
                "Never guess a table name."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "describe_table",
            "description": (
                "Column names, types and nullability for one database table. Call this "
                "before writing a query against a table, so the column names in your SQL "
                "are real ones. Returns no data rows, only the shape of the table."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {"type": "string", "description": "Table name, optionally schema-qualified."},
                    "schema": {"type": "string", "description": "Optional schema name."},
                },
                "required": ["table"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": (
                "Run one read-only SELECT against the customer database and return the rows. "
                "The connection cannot modify anything: INSERT, UPDATE, DELETE and DDL are "
                "refused by the database itself. Prefer aggregates such as count, sum and avg "
                "over pulling raw rows, and always constrain with WHERE and LIMIT."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "One SELECT statement. No semicolons, no multiple statements.",
                    },
                    "max_rows": {
                        "type": "integer",
                        "description": "Most rows to return. Default 200, which is also the ceiling.",
                    },
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_to_excel",
            "description": (
                "THE ONLY WAY to turn a database table into a file. Use it whenever "
                "the user says any of: extract / export / save / copy / download / dump / "
                "'get me' a table, the data, the database, or the records -- and whenever "
                "they ask for database results as a spreadsheet. To save a table, pass "
                "sql = 'SELECT * FROM ' followed by the query_as that list_tables gave "
                "you for that table -- never a table name you have not seen in a tool result. "
                "It runs the SELECT and writes EVERY matching row straight to an .xlsx "
                "without the rows passing through you, so it is not limited to the 200 "
                "rows run_sql shows you -- it writes up to 100,000. You are told the row "
                "count and the column names and nothing else, which is all you need to "
                "tell the user the file is ready. Never try to reach a table with "
                "read_excel, read_pdf, list_files or search_files: those see only the "
                "data folder and a table is not in the data folder. Conversely, if what "
                "the user wants turned into a spreadsheet is already a file in the data "
                "folder -- a PDF, an existing .xlsx, anything list_files or search_files "
                "would find -- that is NOT this tool: there is no database table involved, "
                "so read the file with read_pdf or read_excel and write the result with "
                "write_excel instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": (
                            "One SELECT statement. Quote table and column names exactly as "
                            "list_tables and describe_table gave them to you."
                        ),
                    },
                    "filename": {
                        "type": "string",
                        "description": "Name for the .xlsx file in the data folder.",
                    },
                    "sheet": {
                        "type": "string",
                        "description": "Optional sheet name. Defaults to 'Query'.",
                    },
                    "max_rows": {
                        "type": "integer",
                        "description": "Optional ceiling on rows written. Default 100,000.",
                    },
                },
                "required": ["sql", "filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Find documents by WHAT IS INSIDE THEM, across every PDF and spreadsheet "
                "at once, returning the best matches with a snippet of the matching text. "
                "This is a TEXT match and not a filter: it finds documents containing words, "
                "and cannot compare amounts or dates. For a question with a condition in it, "
                "use this to find candidates and then check each one yourself. "
                "Use this whenever the user describes a document rather than naming "
                "it -- by the job it covers, the company on it, or a word they "
                "remember seeing in it. Search for THEIR words. Never search for a "
                "term that has not appeared in this conversation. "
                "One call searches everything, so never open files one by one looking for "
                "something."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Words likely to appear in the document, such as a name, "
                                       "a reference number or a phrase.",
                    },
                    "limit": {"type": "integer", "description": "Most results to return. Default 8."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_pdf",
            "description": (
                "Extract the text of a PDF in the data folder. Use this before answering "
                "any question about what a PDF says. Never guess at a document's contents."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "File name including the .pdf suffix, for example 'invoice.pdf'.",
                    },
                    "pages": {
                        "type": "string",
                        "description": (
                            "Optional page selection such as '1-3' or '2,5'. "
                            "Omit to read the whole document."
                        ),
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_excel",
            "description": (
                "Read the cells of a worksheet in an .xlsx file in the data folder. "
                "Returns rows as a list of lists, the first usually being the header. "
                "Use this before answering any question about a spreadsheet's contents, "
                "and before appending to one, so you know its columns."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "File name including the .xlsx suffix.",
                    },
                    "sheet": {
                        "type": "string",
                        "description": "Optional sheet name. Omit for the first sheet.",
                    },
                    "max_rows": {
                        "type": "integer",
                        "description": "Most rows to return. Default 200.",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_excel",
            "description": (
                "Create an .xlsx file, or append rows to one that already exists. "
                "Use mode 'append' to add rows to a file, and mode 'overwrite' to create "
                "a new file or replace a sheet's contents. A cell value that starts with "
                "'=' is written as a live Excel formula, so '=SUM(B2:B10)' calculates."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "File name including the .xlsx suffix.",
                    },
                    "rows": {
                        "type": "array",
                        "description": (
                            "The rows to write. Each row is itself a list of cell values, "
                            "so two rows look like [[\"North\", 100], [\"South\", 200]]."
                        ),
                        "items": {"type": "array", "items": {}},
                    },
                    "sheet": {
                        "type": "string",
                        "description": "Optional sheet name. Omit for the first sheet.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["overwrite", "append"],
                        "description": "'append' to add to the end, 'overwrite' to replace.",
                    },
                    "headers": {
                        "type": "array",
                        "description": "Optional header row, written above the data on overwrite.",
                        "items": {"type": "string"},
                    },
                },
                "required": ["filename", "rows"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_pdf",
            "description": (
                "Create a simple text PDF in the data folder. Blank lines separate "
                "paragraphs, and a line starting with '# ' becomes a section heading."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "File name including the .pdf suffix.",
                    },
                    "title": {"type": "string", "description": "Heading at the top of page one."},
                    "body": {"type": "string", "description": "The document text."},
                },
                "required": ["filename", "title", "body"],
            },
        },
    },
]

SYSTEM_PROMPT = """You are a file assistant running on the user's own computer.

You can read and write PDF and Excel files in one folder on that machine, using
the tools provided. Rules:

- Never state the contents of a file you have not read with a tool this turn.
  If you do not know, call the tool. Guessing is the only unacceptable answer.
- Never invent a filename. When the user DESCRIBES a document rather than naming
  it, call search_files with words likely to be inside it. One call searches
  every file at once. Opening documents one by one to see what is in them is
  always the wrong approach and will run out of steps before it finds anything.
  Use list_files when they want to know what exists, search_files when they want
  to know which file contains something.
- After list_files: if exactly one file clearly matches what they asked for,
  use it. If several could match, or none obviously does, stop and ask which
  they mean, listing the candidates by name. Do not pick one and hope. Asking
  costs the user a sentence; guessing wrong costs them a wrong answer or a
  file written in the wrong place.
- THERE ARE TWO SEPARATE PLACES and they share no names. The DATA FOLDER holds
  files -- .pdf and .xlsx -- reached with list_files, search_files, read_pdf,
  read_excel. The DATABASE holds tables, reached with list_tables,
  describe_table, run_sql and query_to_excel. A table is not a file. A file is
  not a table. If the user names something you saw in list_tables, it is in the
  database, no matter which word they used for it -- people say "file",
  "sheet", "record" and "table" for the same thing and mean whichever one they
  are looking at. Never search the data folder for something you know is a
  table, and never invent a filename like Report.xlsx for a table called
  Report.
- Decide which place they mean and ACT. If the words "database", "customer
  database", "client database" or a database name appear, or
  they name something list_tables showed you, it is the database: call
  list_tables and answer. "Show me the files in the customer database" means
  the tables -- they used the word "files" loosely and you know what they
  meant. Asking them to rephrase a question you already understand is
  worse than guessing: guessing costs one wrong tool call, refusing costs them
  their turn. Only ask when a name exists in BOTH places and the two answers
  would differ.
- To put a table into a file, there is exactly one route: query_to_excel. Not
  run_sql then write_excel, which would save 200 rows of a 50,000-row table.
  Not read_excel, which cannot see the database at all.
- For anything about the customer database: list_tables, then describe_table on
  the tables you will use, then run_sql. Never write SQL against a table whose
  columns you have not read. Never invent a column name.
- Both tools give you a `query_as` field, on the table and on every column.
  Paste those into your SQL exactly as written, quotes included. A table named
  Report is a different table from report as far as the server is concerned,
  and a column called Hospital Name written without quotes is a syntax error.
  Never retype a name by hand when query_as gives it to you.
- Database results the user wants as a FILE go through query_to_excel, always.
  Give it the same SQL you would have given run_sql. Do not run the query,
  read the rows, and then retype them into write_excel: you can only see 200
  rows, so you would be writing a fraction of the answer into a file that
  looks complete. query_to_excel has no such limit because the rows never
  reach you.
- When the user asks you to SAVE something you fetched earlier -- "put that in
  a spreadsheet", "save the query you just showed me" -- do not write your
  description of it. Run the same SQL through query_to_excel. Your earlier
  reply was a summary for reading; the file they asked for is the data.
  Silently saving a paragraph where they expected a table is the worst
  possible outcome because it looks like it worked.
- write_pdf and write_excel tell you characters_written. Read it. If the user
  asked for a hundred rows of data and it says a few hundred characters, you
  wrote a summary. Say so and redo it rather than reporting success.
- Every argument you send must come from the user's words or from a tool
  result. Never take a term from a tool's DESCRIPTION and search for it: the
  examples in those descriptions are there to show you the shape of a call,
  not to tell you what the user wants. If you cannot point to where a name
  came from, do not use it.
- A question ABOUT the data is a query with a WHERE clause, never a dump.
  "What is the classification of the Category 2 patient monitor" is
  SELECT "Classification" FROM public."Report"
  WHERE "Category" = 'Category 2' AND "Asset Name" ILIKE '%patient monitor%'.
  Do not fetch rows and read through them looking for the answer: you only see
  a fraction of the table, so an answer found that way is a guess dressed up
  as a fact. Let the server find it.
- If the user's question is about something you have seen in a table, ANSWER
  IT FROM THE TABLE. Do not answer from general knowledge. A question about a
  category, a classification or a status in their data is asking what THEIR
  RECORDS say, not what the term means in the wider world. If you find
  yourself explaining regulatory frameworks, you have misread the question.
- An exported spreadsheet is not a shortcut back to the data. Once you have
  written Report.xlsx, do not read it to answer questions about the table --
  it holds whatever subset you exported, and the table has the rest. Query the
  table.
- The row cap is small and these tables are large. Ask the server to do the
  counting: COUNT, SUM, AVG, GROUP BY, ORDER BY ... LIMIT. Do not pull rows
  back and add them up yourself -- you will only ever see the first few
  hundred, so any total you compute that way is wrong without looking wrong.
  If a result comes back truncated, that is a signal to rewrite the query as
  an aggregate, not to report what you got.
- search_files matches WORDS, not conditions. It cannot compare numbers or
  dates, so a hit is a candidate, never an answer. When the question contains a
  condition (more than, before, at least, between), find the candidates, read
  each one, and then SHOW THE COMPARISON: list every candidate with its actual
  value and say which pass and which fail. Never present a filtered list without
  the numbers you filtered on written next to each item. If a value does not
  satisfy the condition, leave it out and say you checked it.
- Prefer aggregates over raw rows. A question about how many, how much or which
  is the largest is answered with count, sum or max, not by pulling every row
  and counting them yourself. Rows you pull are somebody's real records.
- Image generation takes seconds to minutes, so queue_image returns a job id
  rather than a picture. Say it has started, give the id, and move on. Do not
  call it again, and do not pretend to have seen an image you have not.
- Match the tool to the file type, not to the output you want. A .pdf is read
  with read_pdf even when the answer is going into a spreadsheet. list_files
  tells you which tool each file needs in its read_with field; use it.
- Combining files into one sheet requires them to mean the same thing, not just
  to have the same number of columns. Read the headers first. If the sources
  describe different things, for example regions in one file and invoice line
  items in another, do not force them under one heading. Either use column
  names that are honestly true of every row, such as Source, Item and Amount,
  or give each source its own sheet by calling write_excel once per sheet.
  Never reuse a column name from one file for data it does not describe.
- If two sources turn out to hold identical figures, say so rather than
  silently writing the same numbers twice under different labels.
- Before appending to a spreadsheet, read it, so your new rows match its columns.
- Never announce what you are about to do. Saying "let me read the files" and
  then stopping is a wasted turn. Call the tool in the same reply.
- If the request covers several files or several phases, handle every one of
  them before you answer. Check the list back against what was asked. If you
  could not do part of it, say which part and why, rather than quietly
  answering for the rest.
- If a tool returns an error, read the message, fix the arguments, and try again.
  Do not apologise at length and stop.
- When you have the information, answer in plain prose. Be brief. Quote the
  actual numbers and text you found, not a paraphrase.
- After writing a file, say its name and what you put in it."""


# --------------------------------------------------------------------------
# running one tool call
# --------------------------------------------------------------------------

# Names models reach for that are not the ones our functions use. Remapping
# these costs nothing and saves a whole turn: qwen3 reliably calls write_excel
# with `data` and `sheet_name` rather than `rows` and `sheet`.
ARGUMENT_ALIASES: dict[str, str] = {
    "data": "rows",
    "values": "rows",
    "records": "rows",
    "table": "rows",
    "sheet_name": "sheet",
    "worksheet": "sheet",
    "tab": "sheet",
    "file": "filename",
    "file_name": "filename",
    "path": "filename",
    "filepath": "filename",
    "name": "filename",
    "header": "headers",
    "columns": "headers",
    "content": "body",
    "text": "body",
    "page": "pages",
    "page_range": "pages",
}

# Aliases that are only correct for one tool. `query_as` is the worst offender
# and it is our own doing: list_tables and describe_table hand the model a
# field called query_as holding the text to paste into SQL, and the model
# reasonably concludes that query_as is the name of an argument. Anything a
# tool result names, a tool call may echo back -- so accept it rather than
# lecture the model about a distinction we invented.
TOOL_ARGUMENT_ALIASES: dict[str, dict[str, str]] = {
    "describe_table": {
        "query_as": "table",
        "table_name": "table",
        "relation": "table",
        "schema_name": "schema",
    },
    "run_sql": {
        "query_as": "sql",
        "query": "sql",
        "statement": "sql",
        "sql_query": "sql",
        "limit": "max_rows",
        "row_limit": "max_rows",
    },
    "query_to_excel": {
        "query_as": "sql",
        "query": "sql",
        "statement": "sql",
        "sql_query": "sql",
        "file": "filename",
        "file_name": "filename",
        "name": "filename",
        "path": "filename",
        "sheet_name": "sheet",
        "worksheet": "sheet",
        "limit": "max_rows",
    },
    "job_status": {"id": "job_id", "job": "job_id"},
}


def _coerce_arguments(name: str, function: Callable, arguments: Any) -> dict:
    """Models sometimes send a JSON string, or the wrong names, or nothing."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError as exc:
            raise tools.ToolError(
                f"Arguments for {name} were not valid JSON: {arguments[:200]}"
            ) from exc
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise tools.ToolError(
            f"Arguments for {name} must be an object, got {type(arguments).__name__}."
        )

    parameters = inspect.signature(function).parameters
    accepted = set(parameters)
    local = TOOL_ARGUMENT_ALIASES.get(name, {})

    def resolve(key: str) -> str:
        if key in accepted:
            return key
        # A tool's own alias beats the shared table: ARGUMENT_ALIASES maps
        # "table" to "rows" for write_excel, which would be exactly wrong for
        # describe_table, where "table" is a real argument.
        return local.get(key) or ARGUMENT_ALIASES.get(key, key)

    # Rename what we recognise, but never clobber a key that is already correct.
    renamed: dict[str, Any] = {}
    for key, value in arguments.items():
        target = resolve(key)
        if target in accepted and target not in renamed:
            renamed[target] = value

    dropped = sorted(k for k in arguments if resolve(k) not in accepted)

    required = [
        key for key, parameter in parameters.items()
        if parameter.default is inspect.Parameter.empty
        and parameter.kind not in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD)
    ]
    missing = [key for key in required if key not in renamed]
    if missing:
        # Say what is wrong AND what right looks like, so the retry can succeed.
        signature = ", ".join(
            key if parameters[key].default is inspect.Parameter.empty else f"{key} (optional)"
            for key in parameters
        )
        message = (
            f"{name} is missing the required argument(s): {', '.join(missing)}. "
            f"It accepts: {signature}. You sent: {', '.join(arguments) or '(nothing)'}."
        )
        if dropped:
            message += f" These are not arguments of {name}: {', '.join(dropped)}."
        raise tools.ToolError(message)

    return renamed


def run_tool(name: str, arguments: Any) -> tuple[dict, str | None]:
    """Execute one tool. Returns (result_for_the_model, error_message_or_None)."""
    function = REGISTRY.get(name)
    if function is None:
        known = ", ".join(sorted(REGISTRY))
        return {"error": f"No tool named {name!r}. Available tools: {known}"}, "unknown tool"

    try:
        arguments = _coerce_arguments(name, function, arguments)
        return function(**arguments), None
    except tools.ToolError as exc:
        return {"error": str(exc)}, str(exc)
    except TypeError as exc:
        return {"error": f"Wrong arguments for {name}: {exc}"}, str(exc)
    except Exception as exc:  # noqa: BLE001 - never let a tool crash the request
        return {"error": f"{name} failed unexpectedly: {exc!r}"}, repr(exc)


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------

# Small models sometimes narrate the plan and stop: "Let me start by reading the
# PDFs." That is not an answer, and the user has to say "go on" to get the work.
# Treated as the same failure as an empty reply: one nudge, then move on.
PROMISES = (
    "let me start", "let me begin", "let me read", "let me first", "let me now",
    "i will start", "i will now", "i will begin", "i will read", "i will proceed",
    "i'll start", "i'll begin", "i'll read", "i'll now", "i'll go ahead",
    "i am going to", "i'm going to", "next, i will", "first, i will",
)


def _is_a_promise(text: str) -> bool:
    """Does this read as an intention to act rather than the result of acting?"""
    lowered = text.lower()
    if len(lowered) > 700:  # a long reply is doing something, not stalling
        return False
    return any(phrase in lowered for phrase in PROMISES)

# Tools that only read. When every call in one turn is on this list they can run
# at the same time, which turns "read these five invoices" from five waits into
# one. Anything that writes stays sequential: two writers racing on one file is
# a bug nobody enjoys finding.
READ_ONLY = {
    "list_files", "search_files", "read_pdf", "read_excel",
    "list_tables", "describe_table", "run_sql", "job_status",
}


def _run_calls(calls: list[dict]) -> list[tuple[str, Any, str | None]]:
    """Execute one turn's tool calls, in parallel when it is safe to."""
    prepared = []
    for call in calls:
        block = call.get("function") or {}
        prepared.append((block.get("name", ""), block.get("arguments")))

    if len(prepared) > 1 and all(name in READ_ONLY for name, _ in prepared):
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(prepared), 6)) as pool:
            # Carry the calling thread's context into each worker. A thread does
            # not inherit one, so without this every tool in a parallel turn runs
            # with no tenant set and fails. A fresh copy per submission because a
            # single Context cannot be entered by two threads at once.
            futures = [
                pool.submit(contextvars.copy_context().run, run_tool, name, args)
                for name, args in prepared
            ]
            outcomes = [future.result() for future in futures]
    else:
        outcomes = [run_tool(name, args) for name, args in prepared]

    return [(name, result, error)
            for (name, _), (result, error) in zip(prepared, outcomes)]


TOOL_RESULT_BUDGET = 12_000


def _tool_content(name: str, result: Any) -> str:
    """Serialise one tool result for the model, without handing it broken JSON.

    Slicing the JSON string at a character budget cut it mid-key or mid-escape,
    so a large result reached the model as something that was not JSON at all
    and it had to guess at the tail. Cut the payload instead and say plainly
    that it was cut, so what arrives is always valid and always honest about
    being partial.
    """
    body = json.dumps(result, default=str)
    if len(body) <= TOOL_RESULT_BUDGET:
        return body
    return json.dumps(
        {
            "truncated": True,
            "tool": name,
            "note": (
                f"This result was too large to show in full, so only the first "
                f"{TOOL_RESULT_BUDGET} characters of it are here and the rest is "
                "gone. Do not total or count anything from it. Ask for less: "
                "fewer rows, fewer pages, or an aggregate."
            ),
            "partial_result_text": body[:TOOL_RESULT_BUDGET],
        },
        default=str,
    )


def _tool_calls_of(message: dict) -> list[dict]:
    calls = message.get("tool_calls") or []
    return calls if isinstance(calls, list) else []


def ask(
    question: str,
    history: list[dict] | None = None,
    max_steps: int | None = None,
    system_prompt: str = SYSTEM_PROMPT,
) -> dict:
    """Answer one question, using tools as needed.

    Returns:
        answer   the model's final prose
        steps    what it actually did, in order, for display and debugging
        messages the full conversation, to pass back as `history` next turn
    """
    limit = max_steps or MAX_TOOL_STEPS
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(m for m in history if m.get("role") != "system")
    messages.append({"role": "user", "content": question})

    steps: list[dict] = []
    nudged = False
    # (tool name, exact arguments) -> how many times it has already failed.
    attempts: dict[tuple[str, str], int] = {}

    # One round per model call. The single nudge below gets its own round back,
    # so asking the model to try again never eats the user's tool budget.
    budget = limit + 1
    while budget > 0:
        budget -= 1

        # Before every model call, not once per turn: a single turn can outgrow
        # the window on its own, because each tool result appends to the same
        # list the next call sends.
        dropped = llm.trim_to_window(messages, TOOL_SCHEMAS)
        if dropped:
            # Recorded as a step because that is the one channel the operator
            # UI already renders (app/main.py). It is not a tool call and does
            # not pretend to be one, but it IS something this run did, and the
            # alternative is forgetting part of a conversation with no trace.
            steps.append(
                {
                    "tool": "trim_history",
                    "arguments": {"dropped_messages": dropped},
                    "ok": True,
                    "error": None,
                    "result": {
                        "note": (
                            f"The conversation no longer fits the model's context "
                            f"window, so the {dropped} oldest message(s) were dropped "
                            "to make room for this answer. Earlier turns are gone "
                            "from the model's view; the answer below was built "
                            "without them."
                        ),
                    },
                }
            )

        response = llm.chat(messages, tools=TOOL_SCHEMAS)
        message = response.get("message") or {}
        calls = _tool_calls_of(message)

        # Keep the assistant turn exactly as the model produced it, tool calls
        # included: Ollama needs to see its own request alongside our reply.
        messages.append(
            {
                "role": "assistant",
                "content": message.get("content", "") or "",
                **({"tool_calls": calls} if calls else {}),
            }
        )

        if not calls:
            answer = (message.get("content") or "").strip()

            # Two ways a turn can be no answer at all: it is blank, or it only
            # promises to act. Both get one nudge, on the loop's own budget.
            stalled = not answer or _is_a_promise(answer)
            if answer and not stalled:
                return {"answer": answer, "steps": steps, "messages": messages}

            if not nudged and (steps or _is_a_promise(answer)):
                nudged = True
                budget += 1
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "That was not an answer. Do not describe what you are about to "
                            "do: call the tools now and give me the result. If something is "
                            "stopping you, say plainly what it is and what you need from me."
                        ),
                    }
                )
                continue

            if answer:
                return {"answer": answer, "steps": steps, "messages": messages}

            failures = [step for step in steps if not step["ok"]]
            if failures:
                answer = (
                    "I could not finish that. The last thing that went wrong was:\n\n"
                    f"{failures[-1]['error']}"
                )
            else:
                answer = "The model returned an empty reply. Try asking again."
            return {"answer": answer, "steps": steps, "messages": messages, "empty_reply": True}

        # Order is preserved even when these ran concurrently: the model must see
        # its results in the order it asked for them.
        for (name, result, error), call in zip(_run_calls(calls), calls):
            arguments = (call.get("function") or {}).get("arguments")
            steps.append(
                {
                    "tool": name,
                    "arguments": arguments,
                    "ok": error is None,
                    "error": error,
                    "result": result,
                }
            )

            # A small model that hits an error will often repeat the identical
            # call, word for word, and then repeat it again. Nothing about the
            # world changed between attempts, so nothing about the result will
            # either -- it is a loop, and it burns the user's budget in silence.
            # Say so plainly the second time, because the model clearly is not
            # going to notice on its own.
            if error is not None:
                signature = (name, json.dumps(arguments, sort_keys=True, default=str))
                attempts[signature] = attempts.get(signature, 0) + 1
                if attempts[signature] > 1:
                    result = dict(result)
                    result["error"] = (
                        f"{result.get('error', error)}\n\n"
                        f"You have now called {name} with these exact arguments "
                        f"{attempts[signature]} times and it has failed every "
                        "time. Repeating it will fail again. Change the "
                        "arguments, use a different tool, or tell the user what "
                        "is blocking you and what you need from them. Do not "
                        "make this call again."
                    )

            messages.append(
                {
                    "role": "tool",
                    "tool_name": name,
                    "content": _tool_content(name, result),
                }
            )

    # Fell out of the loop still calling tools.
    return {
        "answer": (
            f"I stopped after {limit} tool calls without reaching an answer. "
            "The steps below show what I tried."
        ),
        "steps": steps,
        "messages": messages,
        "hit_step_limit": True,
    }
