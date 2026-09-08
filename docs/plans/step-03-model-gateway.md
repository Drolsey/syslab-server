# Step 3 — The model gateway

Status: 3.0, 3.1 and 3.2 complete. 3.3 (public exposure) is next.

## 3.2 — the inference plane

`app/gateway.py`, mounted on the same FastAPI process, serving
`/v1/chat/completions` and `/v1/models`. Verified against the live server on
2026-09-08 (app on :8080, vLLM on :8000):

| Check | Result |
|---|---|
| No token / wrong token | 401 both |
| `/v1/models` | advertises `syslab-default` only, never the real model name |
| Unknown model | 404 in OpenAI's error shape |
| Non-streamed tool call | `finish_reason: tool_calls`, arguments as a JSON string |
| Multi-turn: tool result fed back | correct final answer, `finish_reason: stop` |
| Streamed tool call | 12 chunks, stable delta index `0`, arguments reassemble to valid JSON, `[DONE]` terminator, usage chunk present |
| Model name in every streamed chunk | `syslab-default`; zero occurrences of the real name |

The alias round trip is the part worth restating: the caller asks for
`syslab-default`, vLLM is asked for `Qwen/Qwen3-32B-AWQ`, and every response
and every streamed chunk says `syslab-default` on the way back. That is what
makes "changing the served model changes nothing in Secret Manager" true.

**Open follow-up, not blocking:** usage accounting is currently pass-through
only — the `usage` block reaches the caller, but nothing on this side records
per-token spend. Real accounting is worth having before Step 7's measurement
window, which is when someone will actually ask what the hardware served.

## 3.1 — app/llm.py speaks OpenAI

`git diff app/agent.py` was empty for the rewrite itself; all 338 tests pass
unchanged (they mock `llm.chat` directly). Verified against live vLLM with
`scripts/check_agent.py`, first on the Step 1 smoke-test model
(`Qwen3-8B-AWQ`, 5 of 8 scenarios), then on the production candidate
(`Qwen3-32B-AWQ`, 6 of 8).

One real bug surfaced and was fixed: `query_to_excel`'s tool description said
"THE ONLY WAY to turn a database table into a file" and listed "turn X into
a file" as a trigger phrase, with nothing telling the model that a file
already in the data folder is not a database table. Both models hit this —
the 8B model hallucinated a SQL query when asked to sum a spreadsheet it had
already read; the 32B model hallucinated one when asked to turn an invoice
PDF into a spreadsheet. Fixed by adding an explicit exclusion to the tool
description (`app/agent.py`). Pre-existing gap, not introduced by this step.

The remaining 2 of 8 failures on `Qwen3-32B-AWQ` (scenarios 3 and 7) are not
treated as bugs: both times the model reached the correct, verified answer
by a different valid tool path than the test's `expect_tools` requires
(`search_files` + `read_pdf` instead of `list_files`), and scenario 7's
"ambiguity" compares against a fixture (`phase02_sample.pdf`) that has
nothing to do with invoices, left over from an earlier phase's tests. Worth
tightening the test's assertions at some point, but it is not a gateway or
model-quality regression and does not block Step 3.

## 3.0 — vLLM capability matrix

Measured 2026-09-08 against `Qwen/Qwen3-8B-AWQ` on the production box, via
`scripts/check_gateway.py --probe`. This is the model and build that were
running; re-run the probe if either changes before relying on this matrix.

**Finding 1 — tool_choice needed launch flags it wasn't given.** The
container was started with no tool-calling flags at all. Every `tool_choice`
value except `"none"` returned HTTP 400: `"auto" tool choice requires
--enable-auto-tool-choice and --tool-call-parser to be set`. Fixed in
`docker-compose.yml` by adding `--enable-auto-tool-choice` and
`--tool-call-parser hermes` (the parser the architecture plan names as
covering Qwen models). After the fix, the full matrix passes:

| `tool_choice` | Prompt | Result |
|---|---|---|
| `auto` | tool-relevant | Called `get_weather`, `finish_reason: tool_calls` |
| `auto` | unrelated | Did not call it — see Finding 2 below |
| `none` | tool-relevant | Never calls, `finish_reason: stop` |
| `required` | unrelated | Forced a call anyway, `finish_reason: tool_calls` |
| named (`{"name": "get_weather"}`) | unrelated | Forced exactly that function |

**Finding 2 — Qwen3's thinking mode is on by default and will exhaust a
token budget.** The `auto, unrelated prompt` case returned `finish_reason:
"length"` at `max_tokens: 200` — the model spent the whole budget on a
`<think>...</think>` block and never produced an answer. This is the same
behaviour `app/config.py` already documented for Ollama (`OLLAMA_THINK`
defaults to `false` because thinking "roughly triples the wait... keeps the
transcript readable"). vLLM exposes the same control as a non-standard
top-level request field: `chat_template_kwargs: {"enable_thinking": false}`.
Confirmed directly: with it set, the same prompt returns a 7-token answer,
no `<think>` block, `finish_reason: stop`. `app/llm.py`'s `think` parameter
must keep setting this so default behaviour doesn't silently change from
"fast, no reasoning" to "slow, verbose" on the same setting.

**Finding 3 — streaming tool calls are well-formed.** With
`stream_options: {"include_usage": true}`, a streamed tool-calling response:
one tool call, index `0` on every delta chunk (stable, matches the plan's
concern about "whether tool-call deltas carry stable indices"), arguments
arrive across a handful of chunks that concatenate into valid JSON
(`{"city": "Cairo"}`), and a final chunk carries `usage`. No index-tracking
workaround is needed for the single-tool-call case; multi-tool-call-in-one-turn
streaming is untested and should be checked before `app/gateway.py` relies on it.

**Finding 4 — tool_calls arguments arrive as a JSON string, not a dict**
(`arguments_type: "str"` in every probe result). `app/agent.py`'s
`_coerce_arguments` already handles both a string and a dict transparently
(it was written defensively — "Models sometimes send a JSON string"), so
this needs no change on the agent side. It matters only for how
`app/llm.py`'s `chat()` return shape is built: the dict handed back to
`app/agent.py` must keep `message["tool_calls"][i]["function"]["arguments"]`
as whatever vLLM sent (a string), not pre-parse it.

## What this means for 3.1

- `docker-compose.yml` is fixed and deployed with the tool-call parser.
- `app/llm.py`'s new `chat()` must pass `chat_template_kwargs:
  {"enable_thinking": think}` on every request, defaulting `think` to
  `False` exactly as today.
- The response reshape is: take `response["choices"][0]["message"]` and
  return it under the key `"message"`, unchanged otherwise — this preserves
  `app/agent.py`'s `response.get("message")` access and the tool_calls shape
  it already tolerates. No change to `app/agent.py` is needed or expected;
  the sub-step's gate is `git diff app/agent.py` staying empty.
