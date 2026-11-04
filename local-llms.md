# Test local LLMs with WP-Bench

This walkthrough connects WP-Bench to a local inference server and grades the
model's answers in disposable WordPress Playground instances. LM Studio and
Ollama are two examples, not the only options. Other servers exposing a
compatible chat-completions API can use the same configuration pattern; see
[Other inference servers](#other-inference-servers).

The model server generates code; Playground runs WordPress to grade it. No
Docker, native PHP, database server, or cloud API account is required for the
local examples below.

## 1. Install the dependencies

You need Python 3.10+, Node.js 24.18+, npm 11.16+, and a local inference server
such as [LM Studio](https://lmstudio.ai/) or [Ollama](https://ollama.com/). Start
in the root of this WP-Bench checkout and keep the following commands in that
directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ./python
npm ci --prefix runtime/playground
```

In a new terminal, run `source .venv/bin/activate` again before using `wp-bench`.
The Node.js executable on your `PATH` must meet the version requirement; check
with `node --version`. If needed, set `grader.playground_node` in the config
below to the absolute path of a compatible Node.js executable.

## 2. Start a local inference server

Choose one of these options, or use another compatible server. Pick a model
that fits your machine. The model names below are examples, not requirements.
Keep the server running throughout the benchmark and record the model's
quantization, context length, and reasoning settings for comparisons.

### LM Studio

Download and load a model in LM Studio, then start its local API server. Copy
the exact model identifier shown by LM Studio. The config below uses
`qwen3.6-27b-mtp` at `http://localhost:1234/v1`; replace these with your settings.

To check that the server responds and see its model identifiers:

```bash
curl http://localhost:1234/v1/models
```

### Ollama

[Install Ollama](https://ollama.com/download) and start its app or service. If
it is not already serving, run this in a separate terminal:

```bash
ollama serve
```

Download a local model and check its name:

```bash
ollama pull qwen2.5-coder:7b
ollama list
curl http://localhost:11434/v1/models
```

`qwen2.5-coder:7b` is an example local model from the
[Ollama library](https://ollama.com/library/qwen2.5-coder:7b). Use a model that
fits your available memory. Ollama can load a downloaded model when the first
request arrives; allow for that initial load time.

This guide uses Ollama's
[OpenAI-compatible endpoint](https://docs.ollama.com/api/openai-compatibility)
at `http://localhost:11434/v1`. It is a local API protocol, not a request to
OpenAI. Use the exact model tag from `ollama list`, including any size tag.

### Other inference servers

LM Studio and Ollama are examples. A llama.cpp server, vLLM deployment, or
another inference server can also work if it exposes the chat-completions API
expected by the OpenAI-compatible client. Configure the server's actual API
base URL and exact model identifier. Support for generation parameters can
vary, so start with the three-test smoke check below.

WP-Bench uses [LiteLLM](https://docs.litellm.ai/docs/providers) for model calls,
which also offers provider-specific adapters. The examples here deliberately
use `kind: openai-compatible` to keep server-specific routing prefixes out of
the model names saved in results.

## 3. Create the benchmark config

Save this as `wp-bench.local.yaml` in the repository root. This complete example
uses LM Studio; for Ollama, replace just the `model` section as shown below:

```yaml
dataset:
  source: local
  name: wp-core-v1

model:
  kind: openai-compatible
  name: qwen3.6-27b-mtp
  api_base: http://localhost:1234/v1
  temperature: 0
  max_tokens: 8192
  request_timeout: 600

grader:
  kind: playground
  playground_wordpress_version: "7.1"
  timeout_seconds: 90
  setup_timeout_seconds: 600

run:
  suite: wp-core-v1
  execution_concurrency: 1

output:
  path: output/local-llm-playground.json
  jsonl_path: output/local-llm-playground.jsonl
```

For **Ollama**, use this `model` section with the same dataset, grader, run,
and output sections above:

```yaml
model:
  kind: openai-compatible
  name: qwen2.5-coder:7b
  api_base: http://localhost:11434/v1
  temperature: 0
  max_tokens: 8192
  request_timeout: 600
```

For **another compatible server**, replace `name` and `api_base` with its model
identifier and endpoint. Include the API base path (often `/v1`), not the full
`/chat/completions` URL.

`kind: openai-compatible` selects the API protocol, so the model name needs no
`openai/` prefix and is saved unchanged in results. No `OPENAI_API_BASE` or
`OPENAI_API_KEY` variables are needed for the unauthenticated local examples.
If your server requires authentication, add `api_key_env: LOCAL_LLM_TOKEN` to
the `model` section and export that environment variable with your token.
The token value is not stored in result metadata.

`model.request_timeout` allows up to 600 seconds for a model response.
`grader.timeout_seconds` separately limits candidate execution in WordPress,
and `grader.setup_timeout_seconds` limits Playground boot. The first boot
downloads WordPress into Playground's cache and requires internet access.

## 4. Try a small run

```bash
wp-bench run --config wp-bench.local.yaml --limit 3
```

This tests the complete path: send a prompt to the local server, receive generated
code, execute it in a fresh WordPress instance, and save the grading results.
Three tests are a smoke check, not a full benchmark score. The command-line
limit only applies to that invocation.

Optionally verify the runtime using the dataset's reference solutions instead
of calling the model:

```bash
wp-bench run --config wp-bench.local.yaml --check-reference-solution --limit 3
```

## 5. Run the full suite

```bash
wp-bench run --config wp-bench.local.yaml
```

There is no `run.limit` in the config above, so this runs the full `wp-core-v1`
suite. Local reasoning models can take several minutes per answer; allow time
for the complete suite. Each candidate gets a fresh Playground process,
filesystem, and SQLite installation. Concurrency is set to one to keep model
requests and candidate executions serial.

## 6. Understand progress and results

The progress bar advances after a test has been generated and graded. It can
remain at `0%` while the first answer is being generated. Check your inference
server's logs for activity. For example, in LM Studio's logs,
an increasing `n_gen` indicates generation is progressing; `tg` reports tokens
per second. Reasoning tokens can account for most of the wait even when the
final PHP answer is short. A subsequent `POST /v1/chat/completions` request
shows the harness has moved on to another prompt.

The harness suppresses LiteLLM's provider/help banners, including those from
unavailable local-model pricing lookups. Local model cost estimates can be
unavailable; request failures and grading failures are still reported.

Completed runs produce matching timestamped files under `output/`:

- `local-llm-playground_<timestamp>.json`: scores, metadata, and per-test results.
- `local-llm-playground_<timestamp>.jsonl`: one result record per line.

While running, completed tests are saved to a `.jsonl.partial` file. It appears
after the first test is graded and becomes the final JSONL when the run
finishes. An interrupted partial file is useful for inspecting completed tests,
but does not represent a complete suite score.

The leaderboard's [Upload results](https://wpbench-leaderboard.space.fast/)
button can preview the final JSON locally in your browser. Publishing publicly
uses the results repository's
[submission process](https://github.com/joaopamaral/wp-bench-results#publishing-a-run).

For shared comparisons, include the exact model file/revision, quantization,
inference engine version, hardware, context/reasoning settings, and WP-Bench
commit. Playground and Docker results should be identified separately because
their execution environments and resource limits differ. Playground supports
PHP snippets and plugin files; Bash/WP-CLI artifact tests require Docker.
