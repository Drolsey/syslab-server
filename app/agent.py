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

import inspect
import json
from typing import Any, Callable

from app import llm, tools
from app.config import MAX_TOOL_STEPS

# --------------------------------------------------------------------------
# what the model is allowed to call
# --------------------------------------------------------------------------

REGISTRY: dict[str, Callable[..., dict]] = {
    "list_files": tools.list_files,
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
- Never invent a filename. If the user has not given you an exact name, or you
  are not certain the name they gave is real, call list_files FIRST and work
  from what is actually there.
- After list_files: if exactly one file clearly matches what they asked for,
  use it. If several could match, or none obviously does, stop and ask which
  they mean, listing the candidates by name. Do not pick one and hope. Asking
  costs the user a sentence; guessing wrong costs them a wrong answer or a
  file written in the wrong place.
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

    # Rename what we recognise, but never clobber a key that is already correct.
    renamed: dict[str, Any] = {}
    for key, value in arguments.items():
        target = key if key in accepted else ARGUMENT_ALIASES.get(key, key)
        if target in accepted and target not in renamed:
            renamed[target] = value

    dropped = sorted(set(arguments) - {k for k in arguments if k in accepted}
                     - {k for k in arguments if ARGUMENT_ALIASES.get(k) in accepted})

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

    # One round per model call. The single nudge below gets its own round back,
    # so asking the model to try again never eats the user's tool budget.
    budget = limit + 1
    while budget > 0:
        budget -= 1
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

        for call in calls:
            function_block = call.get("function") or {}
            name = function_block.get("name", "")
            arguments = function_block.get("arguments")
            result, error = run_tool(name, arguments)
            steps.append(
                {
                    "tool": name,
                    "arguments": arguments,
                    "ok": error is None,
                    "error": error,
                    "result": result,
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_name": name,
                    "content": json.dumps(result, default=str)[:12_000],
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
