# Contextual chat recovery

`services/chatbot_service.py` generates conversational Discord replies by streaming
`NVIDIA_CHAT_MODEL` through the OpenAI SDK (`AsyncOpenAI`, the same NIM client used
for translation). `main.py` configures the chat model separately from the translation
and KingShot RAG model (`NVIDIA_MODEL`), which also runs the optional tool loop for
direct mentions and the yes/no gate for random replies.

## Failure handling

Direct mentions and replies to the bot normally allow two generation attempts:

1. Use the configured model settings, including thinking mode
   (`extra_body={"chat_template_kwargs": {"enable_thinking": true}}`).
2. If the stream fails or produces no usable reply, repeat the same prompt and
   conversation context with `chat_template_kwargs.enable_thinking=false`.

When server-scoped tools are available, a direct mention first tries the tool loop;
if it is rejected by the API, fails, or produces no usable reply, the two plain
attempts above follow. The tool loop is limited to 40 seconds, so a direct mention
is answered (or gets the fallback) within about 100 seconds at worst. Only a
rejection that mentions tool calling switches tools off for ten minutes; a
request-specific 400 or a temporary provider error does not.

At most two model requests run at once. A direct mention waits up to 30 seconds for
a free slot and then gets the fallback; the random-reply yes/no check is limited to
15 seconds. Random candidates do not use up the author's per-user cooldown slot, and
the channel's random-reply cooldown starts before generation, so two candidates
arriving together produce at most one random reply.

Per-server AI replies and voice replies are opt-in. If the server settings cannot be
loaded, both are treated as off.

The retry override applies to that invocation only. It preserves other configured
chat-template options and does not mutate the shared client's defaults. Thinking
remains enabled for subsequent first attempts. NVIDIA documents the thinking
toggle in the [Nemotron 3.5 Super VL model card](https://build.nvidia.com/nvidia/nemotron-3.5-super-vl-120b-a12b/modelcard).

Empty streams, reasoning-only responses, malformed JSON without a usable plain
answer, and missing, empty, or non-string `reply` fields can trigger the retry.
Existing recovery of plain-text answers and truncated JSON is retained. Partial
answers from interrupted streams are discarded before retrying. Cancellation
propagates without retrying.

If all permitted attempts fail, the bot sends:

> I couldn't generate a reply just now. Please try again in a moment.

The fallback obeys the configured response character limit. Random chat candidates
use one attempt and remain silent on failures or when the model declines to reply.
Internal reasoning is never substituted for a final answer.

## Diagnostics

Every request has a `request_id` shared by its attempts and result logs. Each stream
logs an INFO `Chat stream summary`, including streams that raise an exception:

- Attempt number, completion status, and whether the retry disabled thinking.
- Provider finish reason and input, output, and total token counts.
- Stream chunk count, reasoning chunk/character counts, raw content length,
  cleaned answer length, and elapsed seconds.

Finish reasons and usage can arrive in separate chunks; both are retained. Usage
values are provider totals, not summed on each chunk. Missing metadata is logged
as `None`, not inferred as zero or a successful stop. Reasoning diagnostics contain
counts only. Retry and final-fallback logs include the failure category.

## Debugging discovery and limits

Investigation on 2026-09-05 found an empty final-answer result despite available
conversation history. An isolated replay of the same request succeeded. The old
fallback described generation failures as missing context, and the old logs did
not record the finish reason or token usage. The original upstream cause therefore
could not be established from those logs.

Bounded retries can recover intermittent failures but cannot guarantee provider
availability or output quality. A failed direct request can make up to three
additional generation calls within the shared deadline. No dependencies or
environment settings were added.

## Verification

`tests/test_chatbot_service.py` covers empty and reasoning-only streams, invalid
reply payloads, repeated failures, interrupted streams, cancellation, prompt
preservation, invocation-specific retry options, random reply suppression, and
metadata arriving after the finish-reason chunk. Log assertions verify that
reasoning text is not exposed and that attempts share a request ID.

Run the focused tests and repository checks with:

```bash
uv run --frozen pytest -q tests/test_chatbot_service.py
uv run --frozen ruff check .
uv run --frozen pytest -q
```

On 2026-09-05, all 42 focused tests, all 199 repository tests, and Ruff passed.
An isolated provider check injected an empty first attempt and obtained a valid
answer from NVIDIA on the retry. The actual request payload disabled thinking,
while the shared client's defaults remained unchanged.

## Nemotron 3.5 migration (2026-10-17)

Both `NVIDIA_MODEL` (translation and KingShot RAG) and `NVIDIA_CHAT_MODEL`
(contextual replies) now default to `nvidia/nemotron-3.5-super-vl-120b-a12b`.
Deployment must update both values in `.env.local`; an existing environment value
overrides the code default. Rebuild and recreate the bot service to apply code and
environment changes.

Contextual chat retains thinking, temperature 1, top-p 0.95, and 16,384 completion
tokens, with seed 42 and parallel tool calls enabled as in the provider example.
Translation and RAG explicitly disable thinking because their short completion
budgets must produce final content. Embedding settings and dependencies are unchanged.
See the [NVIDIA model API reference](https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3_5-super-vl-120b-a12b-infer).

The previous Ultra model returned provider degradation errors, overload errors,
and 300-second timeouts. The previous GPT-OSS-120B endpoint returned HTTP 410 Gone.
Pre-deployment checks of the requested 3.5 model also returned provider HTTP 500
(`Failed to generate completions`) for both full service calls and minimal text
requests, including with thinking disabled and optional parameters omitted.
A configuration migration does not resolve an unavailable provider deployment.

Migration validation: Ruff and all 221 repository tests passed on 2026-10-17.
Live provider failures remain separate from passing local request and parsing tests.

## Transient provider failures (2026-10-17)

A real direct mention failed twice with NVIDIA `Failed to generate completions`
(HTTP 500), even though earlier isolated chat and translation checks had succeeded.
Synthetic conversation checks also alternated between successful answers and HTTP
500. A single passing provider check therefore does not establish availability.

Direct replies now retry transient HTTP 429/5xx, degraded-function and overload
responses, transport errors, and timeouts with 2-, 4-, and 8-second backoff, up to
four total attempts. The classifier supports both structured SDK status codes and
ChatNVIDIA's plain exceptions containing bracketed HTTP codes or JSON-like error
bodies. Permanent HTTP errors such as authentication failures are not retried.
Empty or malformed answers and unclassified errors retain the two-attempt limit.
Random reply candidates still make one attempt and stay silent on failure.

Each stream has a 30-second timeout, and all attempts and backoff share one
60-second time budget. Timed-out streams are cancelled. External cancellation also
propagates through streaming and backoff. Retries preserve the prompt, selected
model, request ID, and shared client options; thinking remains disabled after the
first attempt. No new dependencies or model fallback were added. Persistent
provider failure can still exhaust the budget and produce the failure message.

Regression tests cover recovery after repeated 500/503/429/degraded/network
failures, four-attempt exhaustion, permanent error suppression, stream cancellation
on deadline, cancellation during backoff, unchanged shared options and prompts,
and single-attempt behavior for random candidates.

On 2026-10-17, Ruff, all 58 contextual-chat tests, and all 237 repository tests passed.

## OpenAI SDK chat service (2026-11)

The chat service moved from `langchain-nvidia-ai-endpoints` to the OpenAI SDK
(the langchain client did a synchronous model listing at startup). The retry,
fallback, transient-error backoff (up to four attempts, 2/4/8 s, 30 s per attempt,
60 s per reply, no retry on permanent HTTP errors), seed 42 and diagnostics described
above were carried over; the completion ceiling is now 4,096 tokens. Parallel tool
calls are enabled on the tool loop, and the random-reply yes/no check disables
thinking like translation and RAG. `finish_reason` and
`usage` (`prompt_tokens`, `completion_tokens`, `total_tokens`) are read from the
stream chunks when the provider sends them. Direct mentions also have a per-user
cooldown (`CHAT_USER_COOLDOWN_SECONDS`) and at most two concurrent model requests.
Verified with `tests/test_chatbot_service.py` (retry with thinking disabled, retry
after a stream error, fallback after two failures, single attempt for random
replies, cancellation, shared request IDs).
