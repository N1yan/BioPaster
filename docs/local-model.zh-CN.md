# 使用 llama.cpp 本地模型运行 BioPaster

[English](local-model.md) | 中文

本教程通过 llama.cpp 的 Anthropic 兼容 Messages API，将 BioPaster 连接到本地模型。服务支持 `/v1/messages` 时，无需协议转换代理。

## 前置条件

- 已在 Python 3.12 或更高版本环境中安装 BioPaster 及运行依赖。
- `llama-server` 支持 `/v1/messages`、流式输出和工具调用。
- 使用支持工具调用的模型，并配置与其匹配的聊天模板。
- 内存或显存足以运行模型及所配置的上下文。

安装方法请参考 [llama.cpp 服务文档](https://github.com/ggml-org/llama.cpp/tree/master/tools/server)。执行下面的 Python 示例和 CLI 前，先激活安装 BioPaster 的环境。

## 下载模型

安装 [Hugging Face 命令行工具](https://huggingface.co/docs/huggingface_hub/guides/cli)：

```bash
python -m pip install --upgrade huggingface_hub
```

下面以兼容性测试使用的模型为例，从[模型仓库](https://huggingface.co/Jackrong/Qwopus3.5-9B-Coder-GGUF/tree/main)下载对应文件：

```bash
hf download Jackrong/Qwopus3.5-9B-Coder-GGUF \
  Qwopus3.5-9B-coder-Exp-Q4_K_M.gguf \
  --local-dir ./models/qwopus-9b
```

该命令只下载指定文件，不会下载仓库中的全部模型版本。使用其他模型时，将仓库 ID 和文件名替换为其下载页面提供的值。受限仓库可能需要先执行 `hf auth login` 并获得访问许可。

在下一节的启动命令中，将模型参数替换为下载后的路径：

```bash
--model ./models/qwopus-9b/Qwopus3.5-9B-coder-Exp-Q4_K_M.gguf
```

## 启动模型服务

将 `/path/to/model.gguf` 替换为模型文件路径。如果 `llama-server` 不在 PATH 中，使用其实际可执行文件路径。

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

`--jinja` 启用工具调用所需的模板处理。`--alias` 为客户端请求提供统一的模型名称。服务仅监听本机。

根据硬件调整 GPU 层卸载数量；`--n-gpu-layers 0` 表示使用 CPU。保持服务终端运行，等待模型加载完成。在另一个终端检查：

```bash
curl http://127.0.0.1:8001/health
```

就绪时返回 `{"status":"ok"}`。加载期间返回 HTTP 503 属于正常情况。停止服务时，在运行服务的终端按 Ctrl+C。

## 配置 BioPaster

首次使用时，在一个已有目录中运行 `biopaster`，生成 `~/.biopaster/config.json`。程序会显示配置提示并退出。已有配置请先备份。

将初始云端连接替换为下面的本地连接，并将全局 `default_model` 设置为该模型 ID。保留自己需要的 kernel、MCP 等设置。删除不使用的空密钥连接：所有已配置的 provider 都会被校验，即使它没有被选中。

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

模型名称应与服务 alias 一致，上下文预算应与单次请求可用的上下文一致；调整并行槽位后，应核对服务启动日志。使用 `/model` 选择其他已配置的模型或连接，仅影响当前会话。同一 provider 下配置多个模型的方法见[模型配置说明](model-selection.zh-CN.md)。

`api_key: "local"` 是供客户端使用的非空占位值，适用于未开启鉴权的本地服务，无需购买密钥。如果服务开启了鉴权，填写实际配置的密钥。`base_url` 使用服务根地址，由客户端追加 Messages API 路径。

会话标题通过同一个 provider 发起独立请求，关闭 thinking，并设置 2048 token 输出上限。服务是否支持这些选项取决于版本和模型模板。

当前目录默认作为工作区，也可以通过 `biopaster --workspace /path/to/project` 指定其他已有目录。Notebook 默认位于工作区内的 `notebook.ipynb`，无需修改源码。`/new` 沿用这些路径，`/resume` 使用历史会话保存的路径。

## 测试命令行

```bash
mkdir -p ~/biopaster-work
cd ~/biopaster-work
biopaster
```

输入一个简单的工具调用请求：

```text
用 TaskList 查看当前任务。
```

BioPaster 应调用 TaskList 并显示结果。任务列表为空也属于正常结果。

## 常见问题

| 现象 | 检查内容 |
|---|---|
| 连接被拒绝 | 服务进程、端口及 `/health` 响应。 |
| `/v1/messages` 返回 404 | 当前运行的二进制是否支持该接口；只支持 OpenAI API 不代表支持 Anthropic。 |
| 模型加载失败 | 模型路径、可用内存、GPU 驱动和服务报错；适当调整卸载层数或上下文。 |
| 超过上下文限制 | 实际单次请求上下文与 BioPaster 的输入、输出预算。 |
| 不调用工具或重复调用 | `--jinja`、模型模板及指令遵循能力。 |
| 显示用户消息预览而非标题 | 标题生成可能失败；查看日志中的 `session_title_failed`。 |
