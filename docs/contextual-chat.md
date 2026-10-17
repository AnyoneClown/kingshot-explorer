# Contextual chat recovery

`services/chatbot_service.py` generates conversational Discord replies through
`ChatNVIDIA.astream()`. `main.py` configures the chat model separately from the
translation and KingShot RAG model.

## Failure handling

Direct mentions and replies to the bot allow at most two generation attempts:

1. Use the configured model settings, including thinking mode.
2. If the stream fails or produces no usable reply, repeat the same prompt and
   conversation context with `chat_template_kwargs.enable_thinking=false`.

The retry override applies to that invocation only. It preserves other configured
chat-template options and does not mutate the shared client's defaults. Thinking
remains enabled for subsequent first attempts. NVIDIA documents the thinking
toggle in the [Nemotron 3.5 Super VL model card](https://build.nvidia.com/nvidia/nemotron-3.5-super-vl-120b-a12b/modelcard).

Empty streams, reasoning-only responses, malformed JSON without a usable plain
answer, and missing, empty, or non-string `reply` fields can trigger the retry.
Existing recovery of plain-text answers and truncated JSON is retained. Partial
answers from interrupted streams are discarded before retrying. Cancellation
propagates without retrying.

If both attempts fail, the bot sends:

> I couldn't generate a reply just now. Please try again in a moment.

The fallback obeys the configured response character limit. Random chat candidates
use one attempt and remain silent on failures or when the model declines to reply.
Internal reasoning is never substituted for a final answer.

## Diagnostics

Every request has a `request_id` shared by its attempts and result logs. Each stream
logs an INFO summary, including streams that raise an exception:

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

One retry can recover intermittent failures but cannot guarantee provider
availability or output quality. A failed direct request can take up to one
additional generation call. No dependencies or environment settings were added.

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
