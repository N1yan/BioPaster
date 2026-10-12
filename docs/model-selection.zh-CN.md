# 模型配置与切换

[English](model-selection.md) | 中文

BioPaster 从 `~/.biopaster/config.json` 读取模型连接，通过全局 `default_model` 选择启动模型。首次运行会生成配置模板并提示填写后退出；本地部署步骤见[本地模型教程](local-model.zh-CN.md)。

## 一个本地模型

下面的配置对应监听 `127.0.0.1:8001`、模型 alias 为 `biopaster-local` 的本地服务：

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

这是模型连接部分的最小配置。编辑已有文件时，保留需要的 `notebook_kernels`、`mcpServers` 等字段。

| 字段 | 含义与约束 |
| --- | --- |
| `default_model` | 必须匹配已配置模型的 `id` |
| `providers` | 至少包含一个连接；`local` 等名称由你指定 |
| `api_key` | 非空字符串；无鉴权的本地服务可填 `local`，有鉴权时填写实际密钥 |
| `base_url` | 模型服务的基础地址；llama.cpp 使用服务根地址 |
| `models` | 该连接下可选择的模型，至少一项 |
| `id` | 服务实际接受的模型名或 alias；同一连接内不能重复 |
| `context_window` | 正整数，必须大于 `max_output_tokens`，应匹配服务实际可用上下文 |
| `max_output_tokens` | 正整数，限制单次生成的输出 token 数 |

当前 provider 使用 Anthropic SDK，服务需要支持 Messages API、流式响应与工具调用。配置值直接读取，不会对模型连接字段中的 `${变量名}` 自动展开环境变量。

模型条目可以覆盖 provider 级别的 token 预算；未设置时依次使用 provider 的值及内置默认值：`context_window=128000`、`max_output_tokens=32000`。对于小参数本地模型，建议像示例一样显式填写，避免默认预算超出实际资源。

## 配置多个模型或连接

- **同一服务提供多个模型**：在该 provider 的 `models` 数组中增加条目，各自填写模型 ID 和预算。声明条目不会让服务自动下载或加载模型。
- **多个本地服务**：在 `providers` 中增加 `local_a`、`local_b` 等连接，分别填写对应端口和模型列表；确认各服务已启动。
- **不同连接使用相同模型 ID**：启动时按 provider 在 JSON 中的声明顺序，选择第一个匹配 `default_model` 的连接。使用不同 alias 可以避免歧义；交互选择器会显示 provider 和模型名。

只有一个全局 `default_model`，无需配置 `default_provider` 或每个 provider 的默认模型。

所有 provider 都会先校验。即使某个连接没有被选为默认，空密钥、空模型列表或无效预算仍会阻止启动。切换到本地配置时，请删除不使用的初始空密钥连接。

## 在会话中切换

输入 `/model`，使用 ↑/↓ 选择连接和模型，Enter 确认，Esc 取消。

- 每次打开选择器都会重新读取配置文件；可先编辑文件，再调用 `/model`。
- 切换保留对话、工作区和任务，只更新当前使用的模型连接及其预算。
- 切换不会改写配置文件中的 `default_model`；重新启动程序时仍使用文件中的默认值。
- `/new` 和 `/resume` 沿用当前模型连接；恢复会话不会强制切回历史模型。
- 请求执行期间不能切换。配置或客户端初始化失败时，保留原连接。

选择成功表示本地配置和客户端初始化成功，不代表远端密钥、模型或工具调用已经验证。请通过实际任务检查服务响应。

## 思考开关与强度

支持 `output_config.effort` 的 Anthropic 兼容接口，可在对应模型条目中设置：

```json
{
  "id": "qwen3.8-27b",
  "context_window": 128000,
  "max_output_tokens": 32000,
  "enable_thinking": true,
  "reasoning_effort": "xhigh"
}
```

`enable_thinking` 是模型级开关：`true` 开启，`false` 关闭，不填写或设为 `null` 时沿用服务端默认行为。关闭思考时不发送 effort；明确的单次请求开关（包括辅助请求）优先于模型默认值。

`reasoning_effort` 可填写 `low`、`medium`、`high`、`xhigh` 或 `max`；具体档位的支持和映射以服务端为准。Qwen3.8-27B 的当前阿里云接口已验证接受 `low`、`medium`、`xhigh`。不填写或设为 `null` 时不发送强度参数，沿用服务端默认值。不要给不支持该参数的接口添加此配置。

当前名为 `local` 的 provider 按 llama.cpp Messages 接口适配。`reasoning_effort` 通过 `chat_template_kwargs.reasoning_effort` 传入模型模板；Bonsai 2 27B 使用 `medium` 或 `xhigh`，旧 Qwopus 9B 模板不读取该字段，不应为它配置等级。开关通过 `chat_template_kwargs.enable_thinking` 传递，同时保留思考预算的 -1/0 兼容转换。开启时不额外限制思考，仍受总输出和上下文限制。其他 provider 使用 `output_config.effort`；关闭思考时不发送等级。服务端应保留默认 `--reasoning-budget -1`。

本地请求还会同步传入 `chat_template_kwargs.enable_thinking`。Bonsai 2 的实测显示预算 0 本身未关闭思考，而模板开关 false 能关闭，因此实际开关以模板支持为前提。旧 Qwopus 9B 的内置模板不读取该开关，不能保证关闭。预算参数仅保留为兼容处理。示例配置同时展示 local 与 Qwen API；复制时删除不用的 provider，并填写保留连接的密钥和真实地址。

BioPaster 将其通过 `extra_body` 传为 `output_config.effort`，实际请求参数会记录在原生会话日志中。辅助请求明确关闭思考或指定思考预算时，不叠加强度默认值；调用方显式指定的 effort 优先。它不改变 `max_output_tokens`，也不保证实际思考长度。

保存后重新启动，或通过 `/model` 重新选择该模型以加载新配置。`/new`、`/resume` 沿用当前已加载的模型设置，不从历史会话恢复强度。

## 常见配置错误

| 提示或现象 | 处理方式 |
| --- | --- |
| 首次运行创建配置后退出 | 编辑提示路径下的配置，再运行 `biopaster` |
| JSON 行列错误 | 修正对应位置的引号、逗号或括号；原文件不会被默认配置覆盖 |
| `api_key must be a non-empty string` | 填写该连接的密钥或本地占位值；删除不用的连接 |
| `default_model is not present` | 确认 `default_model` 与某个 `models[].id` 完全一致 |
| `Invalid token budgets` | 使用整数，且满足 `context_window > max_output_tokens >= 1` |
| 切换后请求失败 | 检查服务是否运行、接口兼容性、模型 ID、鉴权与服务日志 |

返回[文档目录](README.zh-CN.md)。
