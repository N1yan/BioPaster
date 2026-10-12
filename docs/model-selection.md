# Model configuration and switching

English | [中文](model-selection.zh-CN.md)

BioPaster reads model connections from `~/.biopaster/config.json` and selects its startup model using the global `default_model`. First launch creates a template, displays configuration instructions, and exits. See the [local model guide](local-model.md) for deployment steps.

## One local model

This configuration connects to a local server listening on `127.0.0.1:8001` with the model alias `biopaster-local`:

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

This is a minimal model connection configuration. When editing an existing file, preserve any `notebook_kernels`, `mcpServers`, and other settings you need.

| Field | Meaning and constraints |
| --- | --- |
| `default_model` | Must match a configured model's `id` |
| `providers` | At least one connection; names such as `local` are yours to choose |
| `api_key` | A nonempty string; use `local` for an unauthenticated local server, or its actual key when authentication is enabled |
| `base_url` | The model server's base address; use the server root for llama.cpp |
| `models` | At least one selectable model for this connection |
| `id` | A model name or alias accepted by the server; must be unique within the connection |
| `context_window` | A positive integer greater than `max_output_tokens`, matching the server's available context |
| `max_output_tokens` | A positive integer limiting the tokens generated in one response |

The current provider uses the Anthropic SDK. Servers must support the Messages API, streaming, and tool calls. Model connection fields are read literally; `${VARIABLE}` values are not expanded from environment variables.

Model entries can override token budgets set at provider level. Missing values fall back to the provider's values, then to built-in defaults: `context_window=128000` and `max_output_tokens=32000`. For local small models, set explicit budgets as shown above to avoid exceeding available resources.

## Multiple models or connections

- **Several models on one server:** add entries to the provider's `models` array, each with its model ID and budgets. Declaring an entry does not download or load the model on the server.
- **Several local servers:** add connections such as `local_a` and `local_b` under `providers`, each with its port and model list. Make sure the servers are running.
- **The same model ID on different connections:** at startup, the first provider in JSON declaration order with an ID matching `default_model` is selected. Distinct aliases avoid ambiguity; the interactive picker displays both the provider and model name.

There is one global `default_model`. No `default_provider` or per-provider default model is needed.

Every provider is validated first. An empty key, empty model list, or invalid budget can block startup even when that connection is not the default. When configuring local inference, remove the unused initial connection with an empty key.

## Switch during a session

Enter `/model`, use ↑/↓ to select a connection and model, press Enter to confirm, or Esc to cancel.

- Opening the picker rereads the configuration file, so you can edit it before calling `/model`.
- Switching preserves the conversation, workspace, and tasks. It updates the active model connection and its budgets.
- Switching does not rewrite `default_model` in the configuration file. Restarting the program still uses the configured default.
- `/new` and `/resume` keep the current model connection. Resuming a session does not force a switch to its historical model.
- Switching is unavailable while a request is running. Configuration or client initialization failures leave the original connection active.

A successful selection means local configuration and client initialization succeeded. It does not verify server credentials, model availability, or tool use. Test the server response with an actual task.

## Thinking switch and reasoning effort

For an Anthropic-compatible endpoint supporting `output_config.effort`, add an optional model-level setting:

```json
{
  "id": "qwen3.8-27b",
  "context_window": 128000,
  "max_output_tokens": 32000,
  "enable_thinking": true,
  "reasoning_effort": "xhigh"
}
```

`enable_thinking` accepts `true` (enabled), `false` (disabled), or `null`/omission (server default). Disabled requests omit effort. Explicit per-call thinking controls, including auxiliary calls, override model defaults.

Accepted `reasoning_effort` values are `low`, `medium`, `high`, `xhigh`, and `max`; actual support and aliases depend on the endpoint. The current Alibaba Cloud Qwen3.8-27B endpoint was verified to accept `low`, `medium`, and `xhigh`. Omit the field or use `null` to keep server defaults. Do not configure it for endpoints that do not support this parameter.

The provider named `local` uses the llama.cpp Messages adapter. Model-level `reasoning_effort` is sent as `chat_template_kwargs.reasoning_effort`: use `medium` or `xhigh` for Bonsai 2 27B. The older Qwopus 9B template does not read this setting, so omit it for that model. The switch uses `chat_template_kwargs.enable_thinking` alongside the existing -1/0 budget compatibility mapping. Enabled thinking has no separate budget cap but remains subject to total output and context limits. Other providers use `output_config.effort`. Disabled thinking omits effort. Keep the server default `--reasoning-budget -1`.

Local requests also send `chat_template_kwargs.enable_thinking`. Bonsai 2 testing showed that budget 0 alone did not disable reasoning, while the native template flag did. Effective switching requires template support: the existing Qwopus 9B template ignores this flag, so disabling it is not guaranteed. Budget mapping is retained only for compatibility. The example includes both local and Qwen API connections; remove unused providers and fill in credentials and endpoint addresses for retained connections.

BioPaster sends the value as `output_config.effort` through `extra_body` and logs the request. Explicit per-call effort takes precedence. Auxiliary calls that disable thinking or specify a thinking budget do not inherit this default. Effort does not change the output limit or guarantee a particular reasoning length.

Restart or reselect the model with `/model` after editing. `/new` and `/resume` keep the currently loaded provider settings rather than restoring effort from a historical session.

## Common configuration errors

| Message or symptom | What to do |
| --- | --- |
| First launch creates a configuration and exits | Edit the file at the displayed path, then run `biopaster` again |
| JSON line and column error | Fix the quotes, commas, or brackets at that location; defaults will not overwrite the file |
| `api_key must be a non-empty string` | Supply the connection's key or local placeholder, or remove an unused connection |
| `default_model is not present` | Match `default_model` exactly to a configured `models[].id` |
| `Invalid token budgets` | Use integers satisfying `context_window > max_output_tokens >= 1` |
| Requests fail after switching | Check server availability, API compatibility, model ID, authentication, and server logs |

Back to the [documentation index](README.md).
