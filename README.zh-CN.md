# BioPaster

中文 | [English](README.md)

**用免费的小参数本地模型，开展生物医学研究。**

BioPaster 是面向本地小参数模型的生物医学 AI Agent。将可免费获取和运行的模型部署在自己的设备上，通过自然语言调用文献检索、资料读取和代码执行工具，逐步完成研究任务，无需为本地模型推理支付云端 API 调用费用。

从查阅论文到处理数据，BioPaster 为本地模型提供实际操作研究资料的能力：检索相关文献、读取原文、检查工作区中的数据，编写并执行分析代码，将过程与结果保存到 Notebook。

## 为什么使用 BioPaster

### 免费模型，本地推理

使用许可允许免费运行的小参数模型，在本机完成模型推理。多轮讨论、工具调用和分析迭代无需按云端模型的 token 用量付费，适合反复探索和调整研究任务。

这里的免费指模型获取与运行许可，以及无需云端推理调用费；设备、电力和自行接入的付费服务仍有各自成本。模型权重遵循其自身许可。

### 让小参数模型调用科研工具

BioPaster 将文献、文件和代码工具接入本地模型。模型可以通过检索补充信息、通过原文查证来源、通过执行代码处理数据，让回答建立在实际获取的资料和计算结果上。

| 研究环节 | BioPaster 提供的工具能力 |
| --- | --- |
| 查阅证据 | 检索论文和网页，获取可访问的正文 |
| 准备资料 | 下载文件，读取 PDF、图像与本地文本 |
| 分析数据 | 查找和编辑文件，运行 Bash，通过 Jupyter kernel 执行代码 |
| 检查与继续 | 查看工具结果和 Notebook，保存与恢复会话，维护任务列表 |

### 围绕自己的设备和数据工作

模型服务运行在本机，文件和分析产物保存在自己的工作区。可以选择适合设备资源的模型与量化版本，并通过 MCP 接入数据库和检索服务，通过 Skills 加载具体研究流程。

本地模型推理不需要将对话发送给云端模型提供方；文献检索、下载与外部 MCP 服务仍可能联网，具体取决于任务调用的工具。

## 研究任务示例

```text
检索 HER2 异质性与乳腺癌治疗反应相关的论文，列出来源，并区分论文结果与推测。
```

```text
检查工作区中的表达矩阵和样本信息表，说明可以进行哪些分析、还缺少哪些条件。
先给出方案，暂不运行分析。
```

确认方案后，可以继续要求编写并执行代码、检查输出和调整分析。代码与结果保留在 Notebook 中，便于人工核查和后续复用。

项目正在进行小参数本地模型的任务测试。具体模型、硬件配置和任务表现将在测试完成后补充；当前不承诺所有小参数模型都能可靠完成上述任务。

## 本地运行

需要 Python **3.12 或更高版本**、Git、支持工具调用的小参数模型，以及足以运行所选模型和上下文的内存或显存。下面使用 llama.cpp 提供本地模型服务。

### 1. 安装 BioPaster

以下命令适用于 Linux 的 Bash 终端：

```bash
git clone https://github.com/N1yan/BioPaster.git
cd BioPaster

python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install .

biopaster --version
```

如果使用其他已安装的 Python 3.12+ 解释器，将 `python3.12` 替换为对应命令。后续运行 BioPaster 时，需激活安装它的环境。

Python Notebook 执行依赖可用的 Jupyter kernel。可以在当前环境注册：

```bash
python -m ipykernel install --sys-prefix --name python3 --display-name "Python (BioPaster)"
```

使用网页工具的 Playwright 模式时，还需要安装 Chromium：

```bash
python -m playwright install chromium
```

R、Bash 等额外 Notebook kernels，以及各示例的数据分析依赖，按需单独安装。

### 2. 下载并启动本地模型

按照 [llama.cpp 服务文档](https://github.com/ggml-org/llama.cpp/tree/master/tools/server) 安装 `llama-server`。使用支持 Anthropic Messages API、流式响应和工具调用的版本，并选择带有匹配聊天模板的模型。

从模型发布方下载许可允许免费运行的 GGUF 权重。例如，使用 Hugging Face CLI 下载指定文件：

```bash
python -m pip install huggingface_hub
hf download <模型仓库ID> <模型文件名.gguf> --local-dir ./models
```

将尖括号中的内容替换为模型发布页上的实际仓库 ID 和文件名，再执行命令。模型选择及量化规格应与本机资源相匹配。

保持下面的模型服务在独立终端中运行，将模型路径替换为实际下载位置：

```bash
llama-server \
  --model /path/to/model.gguf \
  --alias biopaster-local \
  --ctx-size 16384 \
  --host 127.0.0.1 \
  --port 8001 \
  --jinja
```

这里使用 16384 token 上下文作为配置示例，请按模型支持范围和设备资源调整。GPU 卸载等参数参照 llama.cpp 文档配置。`--jinja` 启用模板处理，模型本身仍需支持工具调用。

### 3. 将 BioPaster 连接到本地模型

在一个已有目录中运行：

```bash
biopaster
```

首次运行会创建 `~/.biopaster/config.json`，提示填写配置后退出。当前自动生成的模板仍是云端连接示例；本地运行无需购买 API key，将配置改为下面的本地连接即可。

首次配置可以使用以下完整内容。已有配置请先备份，保留自己需要的 kernel、MCP 等设置：

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

`api_key` 中的 `local` 是供客户端使用的非空占位值，适用于未开启鉴权的本地服务，不是付费 API key。如果你为本地服务启用了鉴权，应填写对应密钥。

`default_model` 和模型 `id` 要与 `llama-server --alias` 一致；`context_window` 要与服务可用上下文匹配，`max_output_tokens` 必须小于该值。`base_url` 使用服务根地址，由客户端追加 Messages API 路径。

如果保留多个 provider，每个连接都需要完整有效的配置。请移除不使用的初始空密钥连接，否则仍会触发配置校验错误。

`notebook_kernels` 指定允许代码工具使用的已安装 kernel；默认使用 `python3`。如果没有可用的已配置 kernel，代码执行工具不会加载。

JSON 格式错误会显示行列；空密钥或默认模型不匹配会显示配置错误。程序不会用默认设置覆盖已有文件。

### 4. 开始研究任务

默认以启动命令时的当前目录作为工作区。建议为研究任务建立独立目录：

```bash
mkdir -p ~/biopaster-work
cd ~/biopaster-work
biopaster
```

也可以显式指定一个已有目录：

```bash
biopaster --workspace /path/to/project
```

进入交互界面后，先尝试一个简单的工具请求：

```text
请调用 TaskList，列出当前会话的任务。
```

任务列表为空是正常结果。确认模型能够调用工具后，可以尝试上面的文献查证或数据检查任务。

代码工具默认将 Notebook 保存到工作区的 `notebook.ipynb`；启动程序本身不会创建或覆盖这个 Notebook。分析所需的库需要安装在实际使用的 kernel 环境中。

## 常用命令

| 命令或按键 | 功能 |
| --- | --- |
| `/help` | 查看交互命令帮助 |
| `/model` | 用方向键选择已配置的模型，切换当前会话所用模型 |
| `/new` | 保存已有对话并开始新会话，沿用工作区和 Notebook 路径 |
| `/resume` | 用方向键选择并恢复历史会话 |
| `/permissions` | 查看和调整当前会话的工具权限 |
| `/multiline` | 切换多行输入模式 |
| `/exit` 或 `/quit` | 退出 |
| `Ctrl+O` | 展开或收起当前请求的执行详情 |
| `PgUp` / `PgDn` | 滚动执行详情或最终回答 |

命令行选项可通过 `biopaster --help` 查看。

恢复会话时，工作区、当前工作目录和 Notebook 路径取自历史快照。恢复不会重放工具调用，也不会恢复 kernel 中的变量或临时权限；模型仍使用当前连接配置。

## 执行环境与数据

文件操作、Bash 和 Notebook 代码在本机环境中执行。工具权限交互不等同于操作系统沙箱，请在确认操作内容后授权。

按上面的本地配置，对话及工具返回内容发送到本机模型服务。联网检索、下载和 MCP 工具可能向对应外部服务发送查询或资料，因此本地部署不等于所有任务都完全离线。

本地数据主要保存在以下位置：

| 位置 | 内容 |
| --- | --- |
| `~/.biopaster/config.json` | 模型连接、API key、kernel 和 MCP 配置 |
| `~/.biopaster/sessions/` | 会话快照与事件日志 |
| `~/.biopaster/tasks/` | 会话任务列表 |
| `~/.biopaster/.tool_results/` | 持久化的工具结果 |
| 工作区 | 下载文件、分析产物和 Notebook |

会话日志和工具结果可能包含输入资料、请求内容与输出，分享前请检查其中的信息。

## 示例

[Chroma MCP 示例](examples/chroma_mcp/README.zh-CN.md)：使用 Notebook 准备文档、建立向量库、接入 BioPaster 并评估检索效果。

[Neo4j MCP 示例](examples/neo4j_mcp/README.zh-CN.md)：整理 Open Targets 数据、导入图谱，并通过 Neo4j 官方 MCP 服务查询。

更多资料见 [文档目录](docs/README.zh-CN.md) 和 [TREC 检索评估报告](docs/trec2019_chroma_retrieval.zh-CN.md)。示例各有依赖与数据准备步骤，安装核心包不会自动安装这些示例环境。

## 许可证

BioPaster 使用 [MIT 许可证](LICENSE)，允许商业使用、修改和分发，需保留版权与许可声明。第三方依赖及外部数据遵循各自的许可证和使用条款。
