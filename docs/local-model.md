# Run BioPaster with a local llama.cpp model

English | [中文](local-model.zh-CN.md)

Connect BioPaster to a local model using llama.cpp's Anthropic-compatible Messages API. No protocol-conversion proxy is needed when the server supports `/v1/messages`.

## Requirements

- BioPaster installed in a Python 3.12+ environment with its runtime dependencies.
- A `llama-server` build supporting `/v1/messages`, streaming, and tool calls.
- A model that supports tool calling, with a matching chat template.
- Sufficient memory for the model and configured context.

See the [llama.cpp server documentation](https://github.com/ggml-org/llama.cpp/tree/master/tools/server) for installation. Activate the environment where BioPaster is installed before running the examples.

## Download a model

Install the [Hugging Face CLI](https://huggingface.co/docs/huggingface_hub/guides/cli):

```bash
python -m pip install --upgrade huggingface_hub
```

For a concrete example, download the model file used in the compatibility test from [its repository](https://huggingface.co/Jackrong/Qwopus3.5-9B-Coder-GGUF/tree/main):

```bash
hf download Jackrong/Qwopus3.5-9B-Coder-GGUF \
  Qwopus3.5-9B-coder-Exp-Q4_K_M.gguf \
  --local-dir ./models/qwopus-9b
```

This downloads one model file rather than all variants in the repository. For another model, replace the repository ID and filename with those listed on its download page. Restricted repositories may require `hf auth login` and access approval.

Use the downloaded file in the server command below:

```bash
--model ./models/qwopus-9b/Qwopus3.5-9B-coder-Exp-Q4_K_M.gguf
```

## Start the server

Replace `/path/to/model.gguf` with your model file. Use the executable's actual path if `llama-server` is not on PATH.

```bash
llama-server \
  --model /path/to/model.gguf \
  --alias biopaster-local \
  --n-gpu-layers 99 \
  --ctx-size 16384 \
  --port 8001 \
  --host 127.0.0.1 \
  --jinja
```

`--jinja` enables the template processing required for tool use. The alias supplies a consistent model name. The service listens only on the local machine.

Adjust GPU offloading for your hardware; `--n-gpu-layers 0` runs on CPU. Keep this terminal open and wait for loading to finish. From another terminal:

```bash
curl http://127.0.0.1:8001/health
```

A ready server returns `{"status":"ok"}`. HTTP 503 during loading is expected. Stop the service with Ctrl+C.

## Configure BioPaster

Back up `~/.biopaster/config.json`. Add a local connection and set the global `default_model` to its model ID, preserving the rest of your configuration:

```json
{
  "default_model": "biopaster-local",
  "providers": {
    "local": {
      "api_key": "local",
      "base_url": "http://127.0.0.1:8001",
      "models": [
        {
          "id": "biopaster-local",
          "context_window": 16384,
          "max_output_tokens": 2048
        }
      ]
    }
  }
}
```

Match the model name to the server alias and its context budget to the available context for a single request. Verify the startup log if changing parallel-slot settings. Use `/model` to select another configured model or connection; the selection applies to the current session. See [model configuration](model-selection.md) for multiple models under one provider.

Title generation uses the same provider through a separate request, with thinking disabled and a 2048-token output limit. Server support for these options depends on its version and model template.

The current checkout initializes a fixed workspace and notebook path in `src/biopaster/repl/core.py`. Set them to your own existing workspace before using file or code tools. Configuring the model does not change these paths.

## Test the CLI

```bash
biopaster
```

Enter a simple tool request:

```text
Use TaskList to show the current tasks.
```

BioPaster should call TaskList and display the result. An empty task list is a valid result.

## Troubleshooting

| Symptom | Check |
|---|---|
| Connection refused | Server process, port, and `/health`. |
| `/v1/messages` returns 404 | Support in the running binary. OpenAI compatibility alone does not establish Anthropic support. |
| Model fails to load | Model path, available memory, GPU driver, and server errors. Adjust offloading or context. |
| Context-limit errors | Actual per-request context and BioPaster input/output budgets. |
| Missing or repeated tool calls | `--jinja`, model template, and instruction following. |
| User-message preview instead of a title | Title generation may have failed; check `session_title_failed` in the session log. |
