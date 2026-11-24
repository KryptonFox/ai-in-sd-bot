# Live acceptance check: OpenCode Go / DeepSeek V4.1 Flash

Date: 2026-11-17 (UTC), run window 09:58:27–09:58:44.
Authorization: the user explicitly authorized real OpenCode Go API calls for this acceptance check.
Scope: six real chat-completions requests against the configured Go endpoint, a real math MCP
server, and no Telegram, database, weather, or other external tool calls.

## Configuration checked before the first request

`Settings.load(".env")` resolved the following effective values:

| Item | Observed value |
| --- | --- |
| Base URL | `https://opencode.ai/inference/go/openai/v1` |
| Hostname / path | `opencode.ai` / `/inference/go/openai/v1` |
| Model | `deepseek-v4.1-flash` |
| `LLM_OPENCODE_SESSION` | `true` |
| API key present | yes (value never read into the report or logs) |
| Default mode in `.env` | streaming (`LLM_STREAMING` unset, application default `true`) |

Preflight assertions on hostname, Go path, model, flag, and key presence all passed before any
HTTP request was sent. The existing `OpenAIClient` was used unchanged. Only the existing
`x-opencode-session` / User-Agent header flag was enabled; no thinking, reasoning, or provider
specific parameters were added.

Test-process overrides (not written to `.env`): per-request timeout 90 s, MCP timeout 30 s,
generation cap 4096 tokens, agent max LLM steps 3, tool-call cap 2.

## Request budget

Exactly 6 of the allowed 6 HTTP LLM requests were used:

1. non-streaming public response probe;
2. streaming public response probe;
3. `math_calculate` tool-call request;
4. `math_calculate` continuation;
5. `math_plot` tool-call request;
6. `math_plot` continuation.

Every request had `max_tokens` within the 4096 cap (256 for the two text probes, 4096 for the
four agent requests). No automatic retries were performed.

## Result summary

| Check | HTTP requests | Result | Usage prompt/completion/total | Duration | Public answer |
| --- | ---: | --- | --- | ---: | --- |
| Non-streaming coding probe | 1 | passed | 56 / 39 / 95 | 1,748 ms | `15` |
| Streaming coding probe | 1 | passed | 55 / 53 / 108 | 1,873 ms | `sum(range(1, 11))` |
| `math_calculate` agent cycle | 2 | passed | 2,620 / 90 / 2,710 + 2,776 / 16 / 2,792 | 2,632 ms + 1,407 ms | derivative `3x^2` |
| `math_plot` agent cycle | 2 | passed | 2,628 / 124 / 2,752 + 2,836 / 111 / 2,947 | 2,329 ms + 3,490 ms | plot confirmation |

Total observed usage: 10,971 prompt tokens, 433 completion tokens, 11,404 total tokens.
There were no HTTP 4xx/5xx results, rate limits, permission failures, timeouts, or connection
failures in this run. The client does not expose a success status code; success is established by
the SDK accepting and decoding each response.

## 1–2. Direct non-streaming and streaming probes

Non-streaming:

- `finish_reason=stop`, public content `15` (the synthetic Python expression evaluates to 15).
- Response was decoded by the normal non-streaming `OpenAIClient` path.
- Usage was present and reported above.

Streaming:

- The normal `OpenAIClient` streaming path completed with `finish_reason=stop`.
- The gateway accepted the streamed request, including `stream_options.include_usage`; the
  assembled public content was `sum(range(1, 11))`, and usage was present.
- This confirms gateway-level streaming worked and produced ordinary public assistant content.

The harness did not inspect, print, or retain any reasoning field. Only the public
`LLMResponse.content` was recorded. This run does not establish anything about hidden reasoning
support beyond “the existing header-only path works”; thinking support was intentionally left
unchanged.

## 3–4. `math_calculate` through the real agent and MCP server

The math MCP server was started as a real stdio subprocess. Discovery returned:

- tools: `math_calculate`, `math_solve`, `math_plot`;
- resources: none;
- all three tools reported read-only.

Discovered schemas used for the requests (not hand-copied in the handler):

| Tool | Required fields | Other properties |
| --- | --- | --- |
| `math_calculate` | `expression` | `operation`, `variable`, `order`, `lower`, `upper`, `point`, `direction` |
| `math_plot` | `functions` | `variable`, `x_min`, `x_max` |
| `math_solve` | `equations` | `variables`, `real_only` |

The schema hashes in every agent request matched the discovered `ToolDefinition` objects.

Agent request 3 ended with `finish_reason=tool_calls` and selected `math_calculate`. The recording
tool-client wrapper observed one actual MCP invocation, not merely a textual claim. The call
completed in 1,274 ms and returned a structured result containing `status`, `result`, `exact`,
`numeric`, `expression`, `variable`, `operation`, and `detail`.

The continuation request contained four messages (system, user, assistant tool call, tool result).
The tool result was a structured JSON text message (`source`/`data`), with no PNG or base64
marker. Final response 4 ended with `finish_reason=stop`; `AgentResult.decision` was `tool` and
`reason` was `plain_response`. The model's public answer was the derivative `3x^2`.

There was no evidence that this Go endpoint required `reasoning_content` to be echoed for the
tool continuation: the continuation succeeded using the existing message conversion unchanged.

## 5–6. `math_plot` through the real agent and MCP server

Agent request 5 ended with `finish_reason=tool_calls` and selected `math_plot`. The wrapper
observed one actual MCP invocation, which completed in 282 ms and returned a
`PhotoToolResult`.

- Image count: 1.
- PNG size: 25,988 bytes.
- Dimensions: 640 × 420, RGBA, non-interlaced.
- PNG signature and full image decoding/verification passed.
- SHA-256: `5215af7c9facf6b64d12d2d9031f080a36eb655997a828aa937a4818cc2ce95b`.

Continuation request 6 again carried the tool result as structured JSON text only: no PNG data
URI, no base64 marker, and no raw image payload. It ended with `finish_reason=stop`;
`AgentResult.decision` was `tool` and `reason` was `plain_response`. The public answer confirmed the
plot was built for `y = sin(x)` on `[-3.14159, 3.14159]`.

This separates the layers: tool selection happened at the gateway (`tool_calls`), the actual tool
ran locally and returned a validated image, the final model response was successful public text,
and the image itself was not sent back through the model.

## Compatibility conclusion

For this configuration and synthetic prompts, the OpenCode Go gateway is compatible with the
application's existing header-only `OpenAIClient` path:

- non-streaming chat completions work;
- streaming chat completions work, including usage reporting;
- tool calls and tool-result continuations work for `math_calculate` and `math_plot`;
- the discovered schemas are passed through unchanged;
- read-only math tools return structured results and a valid PNG;
- no 403, 429, or other HTTP error was observed.

This is a small acceptance sample, not a load, long-context, or provider-wide compatibility
guarantee. It also does not exercise error paths, mutation tools, student tools, or Telegram.

## Safety and cleanup

- No Telegram API calls, real user database access, weather calls, or other external tools were
  used.
- No API keys, `.env` lines, raw HTTP bodies, `reasoning_content`, or request text were written to
  the report or logs. The harness retained only safe metadata, usage counts, timings, public
  synthetic answers, and PNG validation data.
- The OpenAI client and math MCP client were closed; no `math_tools` or live-check process
  remained after the run.
- `.env` effective `LLM_OPENCODE_SESSION` is `true`; file mode remained `600`. Only the active
  `LLM_OPENCODE_SESSION=0` value was changed to `1`; other active and commented values were
  preserved. At report time the file contains two active `LLM_OPENCODE_SESSION` lines, both `1`
  (the pre-existing OpenCode block and the default line); the effective last-wins value is `true`.
  No further `.env` edits were made after the coordinator confirmed the effective value.
- No project source files were changed and no commits were created. The only repository change
  from this task is this report.

## Local evidence artifacts

- `/tmp/opencode/live_check_results.json` — safe request metadata, usage, timings, tool calls,
  continuation checks, and image details.
- `/tmp/opencode/math_plot.png` — validated 640 × 420 plot returned by the math server.
