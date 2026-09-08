# Step 3 — The model gateway

Status: in progress. Sub-step 3.0 (measure) is complete; 3.1 (`app/llm.py`
speaks OpenAI) is next.

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
