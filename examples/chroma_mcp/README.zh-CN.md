# Chroma、BM25 与 MCP

中文 | [English](README.md)

用 TREC 2019 临床试验演示完整流程：Markdown 建库，向量与 BM25 混合检索，再通过 MCP 让 BioPaster 搜索并读取原文。

## 文件与运行顺序

| Notebook | 工作 |
| --- | --- |
| [01_prepare_trec.ipynb](01_prepare_trec.ipynb) | 下载历史试验数据，提取 XML，生成 Markdown |
| [02_build_context_db.ipynb](02_build_context_db.ipynb) | 组织背景正文片段，写入 Chroma |
| [03_build_bm25.ipynb](03_build_bm25.ipynb) | 对完整 Markdown 建立、保存和加载 BM25 |
| [04_hybrid_retrieval.ipynb](04_hybrid_retrieval.ipynb) | 加载两路检索器，按 NCT ID 去重并用 RRF 融合 |
| [05_evaluate_retrieval.ipynb](05_evaluate_retrieval.ipynb) | 固定抽取 10 个问题，比较三种检索方式 |

建议的数据位置：

```text
examples/
├── chroma_mcp/
│   ├── 01_prepare_trec.ipynb
│   ├── 02_build_context_db.ipynb
│   ├── 03_build_bm25.ipynb
│   ├── 04_hybrid_retrieval.ipynb
│   ├── 05_evaluate_retrieval.ipynb
│   ├── server.py
│   ├── embedding_profiles.json
│   └── data/trec_pm2019/trial_markdown/
├── chroma_context_db/
└── bm25/bm25_index.pkl
```

五个 Notebook 的工作目录均设为 `examples/chroma_mcp`，默认相对路径对应上面的目录结构。数据库、Markdown 和 BM25 索引由运行生成，不包含在仓库里。如果更改位置，需要同步调整生产和消费这些文件的 Notebook，以及 MCP 配置中的路径。

Notebook 保留的输出来自历史运行，其中机器相关路径已替换为相对路径或 `<python-environment>`；这些输出不代表本次清理后重新执行的结果。正式评估表格与图表未改动。

## 1. 安装环境

使用 Python 3.12。在仓库根目录执行：

```bash
python -m pip install -r examples/chroma_mcp/requirements.txt
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5', device='cpu')"
```

第二条命令将模型下载到本地缓存。Notebook 与 MCP 服务默认使用 CUDA；使用 CPU 时将其中的 `device` 设置改为 `cpu`，服务对应位置是 `get_embedding_model()`。MCP 查询不会自动下载模型。

## 2. 准备原文

以 `examples/chroma_mcp` 为工作目录，依次运行 `01_prepare_trec.ipynb`，包括 **Download benchmark files** 和 **Download trial corpus**。两个下载步骤均使用 `data/trec_pm2019`，与后续提取、转换单元读取的目录一致。Shell 单元需要 Bash 和 `wget`。

四个试验压缩包合计约 1 GB。Notebook 按 qrels 中的 NCT ID 提取 8,567 篇试验，生成 `trial_markdown/NCTxxxxxxxx.md`。Markdown 是后续读取完整原文的来源。

## 3. 建立 Chroma 集合

`02_build_context_db.ipynb` 默认使用以下路径：

```python
md_dir = Path("data/trec_pm2019/trial_markdown")
db_dir = Path("../chroma_context_db")
collection_name = "trec_2019_trials_context"
```

每个片段由标题、疾病、干预信息和一段正文组成。正文来自摘要、详细说明或入组条件；标题、疾病、干预分别最多 60、60、40 tokens。拼接后总长度不超过 450 tokens，正文重叠 50 tokens。

模型为 `BAAI/bge-small-en-v1.5`，向量归一化，索引距离为 cosine。metadata 保留 `nct_id`、`source`、`section`。本次建库得到 43,459 个片段，覆盖全部 8,567 篇试验。已有同名集合会停止建库；需要重建时使用新集合名。

## 4. 建立 BM25 索引

依次运行 `03_build_bm25.ipynb`。每篇 Markdown 作为一条文档，用 `BM25Retriever` 建索引。文档和查询统一转小写，并按 `[a-z0-9]+` 分词。

索引保存到 `../bm25/bm25_index.pkl`，保存单元会自动创建缺失的父目录。索引包括词频统计、按原顺序保存的 metadata 和分词设置。原文仍从 Markdown 读取，因此保存后不要改动文档内容；更新语料时重新构建对应索引。Notebook 最后加载索引并执行一次查询。保存的原文路径相对于 `examples/chroma_mcp`，因此在 `03`、`04` 中重新加载时也应使用这个工作目录。

## 5. 混合检索

打开 `04_hybrid_retrieval.ipynb`，确认 `index_path`、`db_dir`、集合名和模型与建库时一致。

向量侧取 100 个片段，按 NCT ID 保留首次命中；BM25 侧取 100 篇试验。`EnsembleRetriever` 用 RRF 合并两个试验排名，权重各为 0.5，`c=60`。最后显示前 10 个 NCT ID。

## 6. 接入 BioPaster

BioPaster 需要已有可用的模型服务配置。在 `examples/chroma_mcp` 中准备配置：

```bash
cp embedding_profiles.example.json embedding_profiles.json
python -c "import sys; print(sys.executable)"
```

将 `embedding_profiles.json` 中的两个文件路径替换为本机路径：

```json
{
  "trec_2019_trials_context": {
    "model_name": "BAAI/bge-small-en-v1.5",
    "normalize_embeddings": true,
    "bm25_index": "/absolute/path/to/BioPaster/examples/bm25/bm25_index.pkl",
    "documents_dir": "/absolute/path/to/BioPaster/examples/chroma_mcp/data/trec_pm2019/trial_markdown"
  }
}
```

在 `~/.biopaster/config.json` 的 `mcpServers` 中加入以下条目，保留原有模型及其他服务配置。Python 路径须指向安装了 demo 依赖的环境，其他路径也需要替换：

```json
{
  "mcpServers": {
    "chroma": {
      "type": "stdio",
      "command": "/absolute/path/to/python",
      "args": [
        "/absolute/path/to/BioPaster/examples/chroma_mcp/server.py",
        "--data-dir",
        "/absolute/path/to/BioPaster/examples/chroma_context_db",
        "--embedding-profiles",
        "/absolute/path/to/BioPaster/examples/chroma_mcp/embedding_profiles.json"
      ]
    }
  }
}
```

重启 `biopaster`，由它启动 stdio 服务。[server.py](server.py) 提供：

| 工具 | 用途 |
| --- | --- |
| `list_collections` | 列出 Chroma 集合 |
| `search_documents` | 混合检索，返回排名、NCT ID、匹配文本和 metadata |
| `get_trial` | 根据 NCT ID 读取完整 Markdown |

可以依次输入：

> 请从 trec_2019_trials_context 搜索 melanoma BRAF V600E，返回 3 个候选试验。

> 请调用 get_trial 读取第一项试验的原文，列出它的入组条件并注明 NCT ID。

`search_documents` 返回 `retrieval_method: hybrid_rrf` 和 `rank`，不提供 cosine 距离作为融合分数。匹配文本可能是向量片段，也可能是 BM25 命中的文档；需要完整内容时使用 `get_trial`。

## 7. 评估

`05_evaluate_retrieval.ipynb` 默认读取前面生成的数据与索引，配置包括：

```python
output_dir = Path("../../docs/assets/trec2019/hybrid")
sample_size = 10
sample_seed = 42
```

运行导出单元会写入上述正式结果目录。自行实验时，请将 `output_dir` 改为其他目录，以保留参考结果。

评估分别计算 P@10、nDCG@10 和 Recall@100。为了比较 Recall@100，评估会补足每路 100 篇不同试验；服务和 `04` 仍使用向量侧 100 个片段的设置，两者候选预算不同。

[查看实测报告、图表和逐题数据](../../docs/trec2019_chroma_retrieval.zh-CN.md)。

## 换成自己的文档

这个示例的文件名、章节和 `get_trial` 针对 NCT 试验。使用其他文档时，需要按文档结构调整背景字段与正文切分，并统一更换文档标识：向量 metadata、BM25 metadata、去重函数、RRF 的 `id_key` 和原文读取都必须使用同一个 ID。正文可继续保存为 Markdown。更换嵌入模型后重新建立向量集合，并同步模型配置。
