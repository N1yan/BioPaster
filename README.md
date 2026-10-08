# BioPaster

[中文](README.zh-CN.md) | English

**Biomedical research with free small language models, running locally.**

BioPaster is a biomedical AI agent built around locally deployed small language models. Run models that are free to download and use on your own hardware, then use natural language to call literature search, document reading, and code execution tools—without paying cloud API fees for local model inference.

From reviewing papers to working with data, BioPaster gives local models tools to act on research materials: find relevant literature, read source documents, inspect workspace data, write and execute analysis code, and save the code and results in a notebook.

## Why BioPaster

### Free models, local inference

Run small models whose licenses permit free use on your own machine. Conversations, tool calls, and analysis iterations do not incur cloud model charges per token, making it practical to explore and revise research tasks repeatedly.

“Free” refers to obtaining and running the model under its license, with no cloud inference fees. Hardware, electricity, and any paid services you choose to connect still have their own costs. Model weights remain subject to their respective licenses.

### Research tools for small models

BioPaster connects local models to literature, file, and code tools. Models can retrieve additional information, consult original sources, and execute code to work with data, grounding responses in retrieved materials and computed results.

| Research stage | Available tools |
| --- | --- |
| Review evidence | Search papers and websites; retrieve accessible full text |
| Prepare materials | Download files; read PDFs, images, and local text |
| Analyze data | Find and edit files, run Bash commands, and execute code through Jupyter kernels |
| Inspect and continue | Review tool outputs and notebooks, save and resume sessions, and maintain task lists |

### Work with your own hardware and data

The model server runs locally, while files and analysis outputs stay in your workspace. Choose a model and quantization level that fit your hardware, connect databases and retrieval services through MCP, and load research workflows through Skills.

Local inference does not require sending conversations to a cloud model provider. Literature searches, downloads, and external MCP services may still use the network, depending on the tools a task calls.

## Example research tasks

```text
Find papers on HER2 heterogeneity and treatment response in breast cancer.
List the sources and distinguish reported findings from speculation.
```

```text
Inspect the expression matrix and sample metadata in the workspace.
Explain which analyses they support and what information is still missing.
Propose a plan first; do not run the analysis yet.
```

After reviewing the plan, you can ask the agent to write and execute code, inspect outputs, and refine the analysis. Code and results are retained in a notebook for manual review and reuse.

Task testing with local small models is ongoing. Model details, hardware configurations, and task results will be added after testing. Reliable completion of these tasks is not guaranteed for every small model.

## Run locally

You need Python **3.12 or later**, Git, a small model with tool-calling support, and enough RAM or VRAM for the chosen model and context size. The setup below uses llama.cpp as the local model server.

### 1. Install BioPaster

These commands are for Bash on Linux:

```bash
git clone https://github.com/N1yan/BioPaster.git
cd BioPaster

python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install .

biopaster --version
```

If you use another installed Python 3.12+ interpreter, replace `python3.12` with its command. Activate the environment where BioPaster is installed whenever you run it.

Python notebook execution requires an available Jupyter kernel. Register one in the current environment:

```bash
python -m ipykernel install --sys-prefix --name python3 --display-name "Python (BioPaster)"
```

To use the web tool's Playwright mode, also install Chromium:

```bash
python -m playwright install chromium
```

Install additional notebook kernels, such as R or Bash, and dependencies for individual analysis examples separately as needed.

### 2. Download and start a local model

Install `llama-server` following the [llama.cpp server documentation](https://github.com/ggml-org/llama.cpp/tree/master/tools/server). Use a version supporting the Anthropic Messages API, streaming, and tool calls, and select a model with a matching chat template.

Download GGUF weights whose license permits free use from the model publisher. For example, download a specific file with the Hugging Face CLI:

```bash
python -m pip install huggingface_hub
hf download <model-repository-id> <model-filename.gguf> --local-dir ./models
```

Replace the angle-bracket placeholders with the actual repository ID and filename from the model's release page before running the command. Choose a model and quantization level that fit your hardware.

Keep the following model server running in a separate terminal, replacing the model path with the downloaded file's location:

```bash
llama-server \
  --model /path/to/model.gguf \
  --alias biopaster-local \
  --ctx-size 16384 \
  --host 127.0.0.1 \
  --port 8001 \
  --jinja
```

The 16384-token context is an example; adjust it to the model's supported context and your available resources. Configure GPU offloading and other options using the llama.cpp documentation. `--jinja` enables template processing; the model itself must still support tool calling.

### 3. Connect BioPaster to the local model

Run this from an existing directory:

```bash
biopaster
```

On first launch, BioPaster creates `~/.biopaster/config.json`, displays configuration instructions, and exits. The automatically generated template currently contains a cloud connection example. Local use does not require purchasing an API key: replace it with the local connection below.

For a first-time setup, use this complete configuration. If you already have a configuration, back it up and preserve any kernel, MCP, or other settings you need:

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
  },
  "session": {"auto_save": true},
  "notebook_kernels": {"python3": {}},
  "NOTEBOOK_ENV": ""
}
```

The `local` value in `api_key` is a nonempty placeholder for the client when the local server has authentication disabled; it is not a paid API key. If you enable authentication on your server, supply its configured key instead.

Both `default_model` and the model `id` must match `llama-server --alias`. Match `context_window` to the server's available context, with `max_output_tokens` smaller than that value. Use the server root for `base_url`; the client appends the Messages API path.

If you keep multiple providers, every connection must have valid, complete settings. Remove the unused initial connection with an empty key, or configuration validation will still fail.

`notebook_kernels` lists the installed kernels available to the code tool; the default is `python3`. If none of the configured kernels are available, the code execution tool is not loaded.

Invalid JSON reports a line and column. Empty keys and mismatched default models produce configuration errors. Existing files are not overwritten with default settings.

### 4. Start a research task

The directory where you launch BioPaster is the default workspace. Create a separate directory for your research tasks:

```bash
mkdir -p ~/biopaster-work
cd ~/biopaster-work
biopaster
```

Alternatively, specify an existing directory:

```bash
biopaster --workspace /path/to/project
```

Once the interactive interface opens, try a simple tool request:

```text
Call TaskList to list the tasks in the current session.
```

An empty task list is a valid result. Once you have confirmed that the model can call tools, try the literature review or data inspection tasks above.

The code tool uses `notebook.ipynb` in the workspace by default. Starting BioPaster does not create or overwrite this notebook. Install analysis libraries in the environment of the kernel actually used to execute the code.

## Commands

| Command or key | Action |
| --- | --- |
| `/help` | Show interactive command help |
| `/model` | Use arrow keys to select a configured model for the current session |
| `/new` | Save the existing conversation and start a new session, keeping the workspace and notebook paths |
| `/resume` | Use arrow keys to select and resume a saved session |
| `/permissions` | View and adjust tool permissions for the current session |
| `/multiline` | Toggle multiline input |
| `/exit` or `/quit` | Exit |
| `Ctrl+O` | Expand or collapse execution details for the current request |
| `PgUp` / `PgDn` | Scroll execution details or the final answer |

Run `biopaster --help` for command-line options.

Resuming a session restores its workspace, working directory, and notebook path from the saved snapshot. It does not replay tool calls, restore kernel variables, or restore temporary permissions. The current model connection remains in use.

## Execution environment and data

File operations, Bash commands, and notebook code run on your machine. Tool permission prompts are not an operating-system sandbox; review operations before approving them.

With the local configuration above, conversations and tool results are sent to the model server on your machine. Search, download, and MCP tools may send queries or materials to external services, so local deployment does not make every task fully offline.

Local data is primarily stored in these locations:

| Location | Contents |
| --- | --- |
| `~/.biopaster/config.json` | Model connections, API keys, kernel settings, and MCP configuration |
| `~/.biopaster/sessions/` | Session snapshots and event logs |
| `~/.biopaster/tasks/` | Session task lists |
| `~/.biopaster/.tool_results/` | Persisted tool results |
| Workspace | Downloaded files, analysis outputs, and notebooks |

Session logs and tool results may contain input materials, request contents, and outputs. Review them before sharing.

## Examples

[Chroma MCP example](examples/chroma_mcp/README.md): use notebooks to prepare documents, build a vector database, connect it to BioPaster, and evaluate retrieval.

[Neo4j MCP example](examples/neo4j_mcp/README.md): prepare Open Targets data, import a graph, and query it through the official Neo4j MCP server.

See the [documentation index](docs/README.md) and [TREC retrieval evaluation](docs/trec2019_chroma_retrieval.md) for more information. Each example has its own dependencies and data preparation steps; installing the core package does not install these example environments.

## License

BioPaster is available under the [MIT License](LICENSE), which permits commercial use, modification, and distribution while requiring preservation of copyright and license notices. Third-party dependencies and external data remain subject to their respective licenses and terms.
