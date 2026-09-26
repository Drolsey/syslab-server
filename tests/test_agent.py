"""Tests for the tool-calling loop, with the model replaced by a script.

The model itself is not under test here. What is under test is everything
around it: does a tool call actually reach the real function, does the result
get fed back, does a broken call produce a message the model could recover
from, and does the loop always terminate.
"""

from __future__ import annotations

import json

import pytest

from app import agent, config, llm, tools


@pytest.fixture(autouse=True)
def temp_data_dir(tenant_storage):
    yield tenant_storage


def scripted(monkeypatch, *responses):
    """Replace llm.chat with a fixed sequence of model replies.

    Records the messages it was called with, so tests can assert on what the
    model would have seen.
    """
    seen: list[list[dict]] = []
    queue = list(responses)

    def fake_chat(messages, tools=None, **kwargs):
        seen.append([dict(m) for m in messages])
        if not queue:
            raise AssertionError("The loop asked the model more times than expected.")
        return queue.pop(0)

    monkeypatch.setattr(llm, "chat", fake_chat)
    return seen


def says(text: str) -> dict:
    return {"message": {"role": "assistant", "content": text}}


def calls(name: str, arguments) -> dict:
    return {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
        }
    }


# --- the happy path -------------------------------------------------------

def test_a_plain_answer_needs_no_tools(monkeypatch):
    scripted(monkeypatch, says("Hello."))
    result = agent.ask("hi")
    assert result["answer"] == "Hello."
    assert result["steps"] == []


def test_a_tool_call_runs_the_real_function_and_the_result_comes_back(monkeypatch):
    tools.write_excel("sales.xlsx", rows=[["North", 100]], headers=["Region", "Value"])
    seen = scripted(
        monkeypatch,
        calls("read_excel", {"filename": "sales.xlsx"}),
        says("North is 100."),
    )
    result = agent.ask("what is in sales.xlsx?")

    assert result["answer"] == "North is 100."
    assert len(result["steps"]) == 1
    step = result["steps"][0]
    assert step["tool"] == "read_excel" and step["ok"] is True
    assert step["result"]["rows"][1] == ["North", 100]

    # the second call to the model must include the tool result
    tool_messages = [m for m in seen[1] if m["role"] == "tool"]
    assert len(tool_messages) == 1
    assert "North" in tool_messages[0]["content"]


def test_the_assistants_own_tool_call_is_preserved_in_the_transcript(monkeypatch):
    tools.write_excel("s.xlsx", rows=[["a", 1]])
    seen = scripted(monkeypatch, calls("read_excel", {"filename": "s.xlsx"}), says("done"))
    agent.ask("read it")
    assistant_turns = [m for m in seen[1] if m["role"] == "assistant"]
    assert assistant_turns and "tool_calls" in assistant_turns[0]


def test_history_is_carried_and_the_old_system_prompt_is_dropped(monkeypatch):
    seen = scripted(monkeypatch, says("second answer"))
    history = [
        {"role": "system", "content": "an old system prompt"},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
    ]
    agent.ask("second question", history=history)
    roles = [m["role"] for m in seen[0]]
    assert roles.count("system") == 1
    assert seen[0][0]["content"] == agent.SYSTEM_PROMPT
    assert "first question" in [m["content"] for m in seen[0]]


# --- recovery -------------------------------------------------------------

def test_a_tool_error_is_handed_back_as_text_not_raised(monkeypatch):
    tools.write_excel("real.xlsx", rows=[["a", 1]])
    seen = scripted(
        monkeypatch,
        calls("read_excel", {"filename": "imaginary.xlsx"}),
        calls("read_excel", {"filename": "real.xlsx"}),
        says("Found it."),
    )
    result = agent.ask("read the sales file")

    assert result["answer"] == "Found it."
    assert result["steps"][0]["ok"] is False
    # the model is told what went wrong AND what it could have used instead
    error_text = [m for m in seen[1] if m["role"] == "tool"][0]["content"]
    assert "imaginary.xlsx" in error_text and "real.xlsx" in error_text


def test_an_unsafe_path_becomes_an_error_message_not_a_read(monkeypatch, tmp_path):
    secret = tmp_path.parent / "secret.pdf"
    secret.write_bytes(b"%PDF-1.4 not yours")
    scripted(monkeypatch, calls("read_pdf", {"filename": "../secret.pdf"}), says("I cannot."))
    result = agent.ask("read the secret")
    assert result["steps"][0]["ok"] is False
    assert "outside the data folder" in result["steps"][0]["error"]


def test_an_unknown_tool_name_is_reported_with_the_real_ones(monkeypatch):
    scripted(monkeypatch, calls("delete_everything", {}), says("No such tool."))
    result = agent.ask("delete my files")
    assert result["steps"][0]["ok"] is False
    assert "read_excel" in result["steps"][0]["result"]["error"]


def test_arguments_as_a_json_string_are_parsed(monkeypatch):
    tools.write_excel("s.xlsx", rows=[["a", 1]])
    scripted(monkeypatch, calls("read_excel", json.dumps({"filename": "s.xlsx"})), says("ok"))
    assert agent.ask("read")["steps"][0]["ok"] is True


def test_an_unexpected_extra_argument_is_dropped_rather_than_failing(monkeypatch):
    tools.write_excel("s.xlsx", rows=[["a", 1]])
    scripted(
        monkeypatch,
        calls("read_excel", {"filename": "s.xlsx", "colour": "blue"}),
        says("ok"),
    )
    assert agent.ask("read")["steps"][0]["ok"] is True


def test_a_missing_required_argument_is_a_readable_message(monkeypatch):
    scripted(monkeypatch, calls("read_excel", {}), says("I need a filename."))
    step = agent.ask("read a file")["steps"][0]
    assert step["ok"] is False
    assert "filename" in step["result"]["error"]


def test_a_tool_that_crashes_does_not_crash_the_request(monkeypatch):
    def explode(**_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setitem(agent.REGISTRY, "list_files", explode)
    scripted(monkeypatch, calls("list_files", {}), says("Something went wrong."))
    result = agent.ask("list my files")
    assert result["steps"][0]["ok"] is False
    assert "disk on fire" in result["steps"][0]["result"]["error"]


# --- termination ----------------------------------------------------------

def test_the_loop_stops_at_the_step_limit(monkeypatch):
    scripted(monkeypatch, *[calls("list_files", {}) for _ in range(4)])
    result = agent.ask("loop forever", max_steps=3)
    assert result.get("hit_step_limit") is True
    assert len(result["steps"]) == 4
    assert "stopped after 3 tool calls" in result["answer"]


def test_two_tool_calls_in_one_turn_both_run(monkeypatch):
    tools.write_excel("a.xlsx", rows=[["a", 1]])
    tools.write_excel("b.xlsx", rows=[["b", 2]])
    two = {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "read_excel", "arguments": {"filename": "a.xlsx"}}},
                {"function": {"name": "read_excel", "arguments": {"filename": "b.xlsx"}}},
            ],
        }
    }
    scripted(monkeypatch, two, says("Both read."))
    result = agent.ask("read both")
    assert len(result["steps"]) == 2
    assert all(s["ok"] for s in result["steps"])


# --- schema sanity --------------------------------------------------------

def test_every_advertised_tool_exists_and_every_tool_is_advertised():
    advertised = {s["function"]["name"] for s in agent.TOOL_SCHEMAS}
    assert advertised == set(agent.REGISTRY)


def test_every_required_parameter_is_a_real_function_argument():
    import inspect

    for schema in agent.TOOL_SCHEMAS:
        block = schema["function"]
        signature = inspect.signature(agent.REGISTRY[block["name"]])
        for name in block["parameters"].get("properties", {}):
            assert name in signature.parameters, f"{block['name']} advertises unknown arg {name}"


# --- regressions from the first real multi-file session (1 Sep 2026) ------

def test_write_excel_called_with_data_and_sheet_name_still_works(monkeypatch):
    """qwen3 reliably reaches for `data` and `sheet_name`. Remap, do not fail."""
    scripted(
        monkeypatch,
        calls("write_excel", {
            "filename": "out.xlsx",
            "sheet_name": "Invoice Data",
            "data": [["Invoice", "Total"], ["SL-4417-B", 18640]],
        }),
        says("Written."),
    )
    result = agent.ask("build me a sheet")
    step = result["steps"][0]
    assert step["ok"] is True, step["error"]
    assert step["result"]["sheet"] == "Invoice Data"
    assert tools.read_excel("out.xlsx")["rows"][1] == ["SL-4417-B", 18640]


@pytest.mark.parametrize(
    "alias,correct",
    [("file", "filename"), ("file_name", "filename"), ("path", "filename")],
)
def test_common_filename_aliases_are_remapped(monkeypatch, alias, correct):
    tools.write_excel("book.xlsx", rows=[["a", 1]])
    scripted(monkeypatch, calls("read_excel", {alias: "book.xlsx"}), says("ok"))
    assert agent.ask("read it")["steps"][0]["ok"] is True


def test_a_correct_key_is_not_clobbered_by_an_alias(monkeypatch):
    tools.write_excel("right.xlsx", rows=[["a", 1]])
    scripted(
        monkeypatch,
        calls("read_excel", {"filename": "right.xlsx", "file": "wrong.xlsx"}),
        says("ok"),
    )
    step = agent.ask("read it")["steps"][0]
    assert step["ok"] is True
    assert step["result"]["file"] == "right.xlsx"


def test_a_missing_required_argument_explains_the_whole_signature(monkeypatch):
    scripted(monkeypatch, calls("write_excel", {"filename": "out.xlsx", "nonsense": 1}), says("."))
    error = agent.ask("write something")["steps"][0]["result"]["error"]
    assert "rows" in error                 # what is missing
    assert "It accepts:" in error          # what right looks like
    assert "nonsense" in error             # what it sent that was not real


def test_a_missing_file_error_suggests_the_closest_names():
    tools.write_pdf("phase02_sample.pdf", title="T", body="body")
    tools.write_pdf("phase03_invoice.pdf", title="T", body="body")
    with pytest.raises(tools.ToolError) as caught:
        tools.read_pdf("phase-02-invoice.pdf")
    message = str(caught.value)
    assert "closest names" in message
    assert "ask the user" in message


def test_an_empty_reply_after_a_failure_gets_one_nudge_then_answers(monkeypatch):
    seen = scripted(
        monkeypatch,
        calls("read_excel", {"filename": "ghost.xlsx"}),
        says(""),                     # the blank turn that produced "(no answer)"
        says("I could not find that file. Which did you mean?"),
    )
    result = agent.ask("summarise the sheet")
    assert result["answer"] == "I could not find that file. Which did you mean?"
    nudges = [m for m in seen[2] if m["role"] == "user" and "not an answer" in m["content"]]
    assert len(nudges) == 1


def test_two_empty_replies_fall_back_to_the_real_error_not_a_blank(monkeypatch):
    scripted(
        monkeypatch,
        calls("read_excel", {"filename": "ghost.xlsx"}),
        says(""),
        says(""),
    )
    result = agent.ask("summarise the sheet")
    assert result.get("empty_reply") is True
    assert result["answer"]
    assert "ghost.xlsx" in result["answer"]


def test_an_empty_reply_with_no_tools_used_is_not_nudged_forever(monkeypatch):
    scripted(monkeypatch, says(""))
    result = agent.ask("hello")
    assert result.get("empty_reply") is True
    assert "empty reply" in result["answer"]


def test_the_system_prompt_tells_it_to_ask_rather_than_guess():
    assert "ask which" in agent.SYSTEM_PROMPT
    assert "Never invent a filename" in agent.SYSTEM_PROMPT


def test_a_turn_that_only_promises_to_act_is_nudged(monkeypatch):
    seen = scripted(
        monkeypatch,
        says("The user mentioned the files are PDFs, so I will proceed to extract "
             "the text. Let me start by reading the PDF files."),
        calls("read_pdf", {"filename": "doc.pdf"}),
        says("The total is 18,640 AED."),
    )
    tools.write_pdf("doc.pdf", title="Invoice", body="Total 18,640 AED")
    result = agent.ask("they are pdf files")
    assert result["answer"] == "The total is 18,640 AED."
    assert len(result["steps"]) == 1
    nudges = [m for m in seen[1] if m["role"] == "user" and "not an answer" in m["content"]]
    assert len(nudges) == 1


def test_a_promise_that_is_nudged_twice_is_returned_rather_than_looping(monkeypatch):
    scripted(monkeypatch, says("Let me start by reading them."), says("I will now read them."))
    result = agent.ask("go")
    assert "read them" in result["answer"]


def test_a_real_answer_that_merely_mentions_reading_is_not_nudged(monkeypatch):
    scripted(monkeypatch, says("I read the invoice and the total is 18,640 AED."))
    result = agent.ask("what is the total?")
    assert result["answer"].startswith("I read the invoice")


def test_a_long_reply_is_never_treated_as_a_stall(monkeypatch):
    long_answer = "Let me start by saying: " + ("the invoice totals 18,640 AED. " * 40)
    scripted(monkeypatch, says(long_answer))
    assert agent.ask("summarise")["answer"] == long_answer.strip()


def test_the_prompt_forbids_announcing_and_demands_full_coverage():
    assert "Never announce what you are about to do" in agent.SYSTEM_PROMPT
    assert "several files" in agent.SYSTEM_PROMPT
    assert "Match the tool to the file type" in agent.SYSTEM_PROMPT


def test_the_listing_schema_points_the_model_at_read_with():
    listing = next(s for s in agent.TOOL_SCHEMAS if s["function"]["name"] == "list_files")
    assert "read_with" in listing["function"]["description"]


def test_the_prompt_covers_combining_mismatched_sources():
    assert "Never reuse a column name from one file" in agent.SYSTEM_PROMPT
    assert "identical figures" in agent.SYSTEM_PROMPT


def test_several_read_only_calls_in_one_turn_run_concurrently(monkeypatch):
    """Reading five documents should cost one wait, not five."""
    import time

    def slow_read(**_kwargs):
        time.sleep(0.25)
        return {"ok": True}

    monkeypatch.setitem(agent.REGISTRY, "read_pdf", slow_read)
    calls = [{"function": {"name": "read_pdf", "arguments": {"filename": f"{n}.pdf"}}}
             for n in range(5)]
    started = time.time()
    outcomes = agent._run_calls(calls)
    elapsed = time.time() - started
    assert len(outcomes) == 5
    assert all(error is None for _, _, error in outcomes)
    assert elapsed < 0.9, f"took {elapsed:.2f}s, so they ran one after another"


def test_a_turn_containing_a_write_stays_sequential(monkeypatch):
    """Two writers racing on one file is a bug nobody enjoys finding."""
    order: list[str] = []

    def slow(name):
        def run(**_kwargs):
            order.append(f"start {name}")
            time.sleep(0.1)
            order.append(f"end {name}")
            return {"ok": True}
        return run

    import time
    monkeypatch.setitem(agent.REGISTRY, "read_pdf", slow("read"))
    monkeypatch.setitem(agent.REGISTRY, "write_excel", slow("write"))
    agent._run_calls([
        {"function": {"name": "read_pdf", "arguments": {}}},
        {"function": {"name": "write_excel", "arguments": {}}},
    ])
    assert order == ["start read", "end read", "start write", "end write"]


def test_results_keep_the_order_the_model_asked_in(monkeypatch):
    monkeypatch.setitem(agent.REGISTRY, "read_pdf", lambda filename=None, **_: {"f": filename})
    outcomes = agent._run_calls([
        {"function": {"name": "read_pdf", "arguments": {"filename": f"{n}.pdf"}}}
        for n in ("a", "b", "c")
    ])
    assert [result["f"] for _, result, _ in outcomes] == ["a.pdf", "b.pdf", "c.pdf"]


def test_the_prompt_demands_the_comparison_be_shown():
    """A text hit is a candidate, not an answer. Listing 11,200 under 'over
    12,000' is the failure this rule exists to prevent."""
    assert "search_files matches WORDS, not conditions" in agent.SYSTEM_PROMPT
    assert "SHOW THE COMPARISON" in agent.SYSTEM_PROMPT


def test_search_results_say_they_are_matches_and_not_answers():
    from app import search

    schema = next(s for s in agent.TOOL_SCHEMAS if s["function"]["name"] == "search_files")
    assert "not a filter" in schema["function"]["description"]


def test_no_tool_description_contains_a_copyable_literal():
    """An example inside a tool description is indistinguishable from an argument.

    This is not hypothetical. The search_files description used to read
    "whichever file mentions Meridian" as an illustration, and the model
    searched for "Meridian" in a conversation where the user had never said
    the word -- then reported eight matching invoices as if they were the
    answer to a question about database tables.

    Tool descriptions may show the SHAPE of a call. They must not contain a
    name, company or identifier that could be sent as an argument.
    """
    import json
    from app import agent

    blob = json.dumps(agent.TOOL_SCHEMAS)
    for literal in ("Meridian", "calibration job", 'public."Report"', "Report"):
        assert literal not in blob, (
            f"{literal!r} appears in a tool description; the model will copy it"
        )


def test_a_large_tool_result_still_reaches_the_model_as_json():
    # Slicing the serialised result at a character budget cut it mid-key, so a
    # big result arrived as something that was not JSON and the model guessed
    # at the tail.
    import json as _json

    big = {"rows": [{"name": "x" * 200, "n": i} for i in range(400)]}
    content = agent._tool_content("read_excel", big)
    parsed = _json.loads(content)          # the point of the test
    assert parsed["truncated"] is True
    assert "read_excel" in parsed["tool"]

    small = {"ok": True}
    assert _json.loads(agent._tool_content("list_files", small)) == small


# --- History trimming against the context window ---------------------------
#
# The failure these cover, seen 9 September 2026 in the operator UI: nothing
# trimmed conversation history, so a long chat eventually sent a prompt larger
# than the model's window and vLLM refused it -- "you requested 0 output tokens
# and your prompt contains at least 8193 input tokens". Terminal, because every
# following message is larger than the one that just failed.


@pytest.fixture()
def window(monkeypatch):
    """Set the model's context window, the way a test that cares must."""

    def set_to(size: int | None):
        monkeypatch.setattr(llm, "model_window", lambda model: size)

    return set_to


def _long_history(turns: int) -> list[dict]:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 400}
        for i in range(turns)
    ]


def test_an_unknown_window_changes_nothing(window):
    # The documented contract for None: never invent a limit, because an
    # assumed window silently discards conversation a working server accepts.
    window(None)
    messages = [{"role": "system", "content": "s"}] + _long_history(40)
    before = [dict(m) for m in messages]

    assert llm.trim_to_window(messages, None) == 0
    assert messages == before


def test_trimming_drops_the_oldest_and_keeps_system_and_newest(window):
    window(2048)
    messages = [{"role": "system", "content": "system prompt"}]
    messages += _long_history(40)
    messages.append({"role": "user", "content": "the question being asked now"})
    newest = dict(messages[-1])

    dropped = llm.trim_to_window(messages, None)

    assert dropped > 0
    assert messages[0]["role"] == "system"        # never dropped
    assert messages[-1] == newest                 # never dropped
    assert len(messages) == 42 - dropped
    # It stopped as soon as there was room for a reply, not later.
    assert llm.estimate_prompt_tokens(messages) + llm.MIN_REPLY_TOKENS <= 2048


def test_trimming_never_leaves_an_orphan_tool_result(window):
    # A `tool` message whose assistant turn was dropped is rejected outright,
    # which would trade one HTTP 400 for a different one.
    window(1024)
    messages = [{"role": "system", "content": "s"}]
    for _ in range(8):
        messages.append({"role": "user", "content": "q" * 300})
        messages.append(
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]}
        )
        messages.append({"role": "tool", "tool_name": "run_sql", "content": "r" * 300})
    messages.append({"role": "user", "content": "now"})

    llm.trim_to_window(messages, None)

    assert messages[1]["role"] != "tool", "the conversation opens with an orphan"
    for i, message in enumerate(messages):
        if message.get("role") == "tool":
            assert messages[i - 1].get("tool_calls"), (
                "a tool result survived without the assistant turn that asked for it"
            )


def test_a_prompt_that_cannot_be_trimmed_is_left_for_the_server_to_reject(window):
    # One enormous message with nothing droppable behind it. The model server
    # states this precisely; guessing here replaces a precise error with a
    # vaguer one.
    window(512)
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "x" * 100_000},
    ]

    assert llm.trim_to_window(messages, None) == 0
    assert len(messages) == 2


def test_the_loop_trims_and_says_so_in_the_steps(monkeypatch, window):
    # 16384 because that is what the box actually serves (--max-model-len in
    # docker-compose.yml). A smaller number here would not be a stricter test,
    # it would be an impossible one: the system prompt and the twelve tool
    # schemas are ~6000 estimated tokens before the conversation starts, and
    # neither of them is droppable. The history is sized to overflow that real
    # window rather than a convenient one -- a test that only trims on a window
    # nothing serves proves nothing about the server that exists.
    window(16384)
    seen = scripted(monkeypatch, says("the answer"))

    outcome = agent.ask("and now?", history=_long_history(120))

    # It fitted by the time the model was called, which is the whole point.
    sent = seen[0]
    assert llm.estimate_prompt_tokens(sent, agent.TOOL_SCHEMAS) + llm.MIN_REPLY_TOKENS <= 16384
    assert sent[0]["role"] == "system"
    assert sent[-1]["content"] == "and now?"

    # And it is visible, rather than a conversation quietly forgetting.
    trims = [s for s in outcome["steps"] if s["tool"] == "trim_history"]
    assert len(trims) == 1
    assert trims[0]["ok"] is True
    assert trims[0]["arguments"]["dropped_messages"] > 0
    assert outcome["answer"] == "the answer"


def test_a_conversation_that_fits_is_never_trimmed(monkeypatch, window):
    window(32768)
    scripted(monkeypatch, says("fine"))

    outcome = agent.ask("hello", history=[{"role": "user", "content": "hi"}])

    assert [s for s in outcome["steps"] if s["tool"] == "trim_history"] == []
