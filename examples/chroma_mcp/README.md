# Chroma, BM25 and MCP

[中文](README.zh-CN.md) | English

A clinical-trial retrieval demo using TREC 2019: index Markdown, combine vector search with BM25, then let BioPaster search and read the original documents through MCP.

## Files and execution order

| Notebook | Purpose |
| --- | --- |
| [01_prepare_trec.ipynb](01_prepare_trec.ipynb) | Download the historical corpus, extract XML and write Markdown |
| [02_build_context_db.ipynb](02_build_context_db.ipynb) | Build context-enriched chunks and index them in Chroma |
| [03_build_bm25.ipynb](03_build_bm25.ipynb) | Build, save and reload a BM25 index over complete Markdown documents |
| [04_hybrid_retrieval.ipynb](04_hybrid_retrieval.ipynb) | Load both retrievers, deduplicate trials and fuse their rankings |
| [05_evaluate_retrieval.ipynb](05_evaluate_retrieval.ipynb) | Evaluate three retrieval methods on a fixed 10-query sample |

Suggested data layout:

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

Use `examples/chroma_mcp` as the working directory for all five notebooks. Their default relative paths follow the layout above. The database, Markdown corpus and BM25 index are generated locally and are not included in the repository. If you change their locations, update both the producing and consuming notebooks, plus the MCP profile paths.

Saved notebook outputs are from earlier runs; machine-specific paths have been replaced with portable paths or `<python-environment>`. They are not results from rerunning the notebooks after this cleanup. Published evaluation tables and charts are unchanged.

## 1. Install dependencies

Use Python 3.12. From the repository root:

```bash
python -m pip install -r examples/chroma_mcp/requirements.txt
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5', device='cpu')"
```

The second command downloads the embedding model into the local cache. The notebooks and MCP server use CUDA by default. For CPU execution, change their `device` settings to `cpu`; the server setting is in `get_embedding_model()`. Search loads the cached model without downloading it.

## 2. Prepare source documents

With `examples/chroma_mcp` as the working directory, run `01_prepare_trec.ipynb` in order, including **Download benchmark files** and **Download trial corpus**. Both download steps use `data/trec_pm2019`, the directory read by the extraction and conversion cells. The shell cells require Bash and `wget`.

The four trial archives total approximately 1 GB. The notebook selects 8,567 trials referenced by qrels and creates `trial_markdown/NCTxxxxxxxx.md`. These Markdown files remain the full-text source.

## 3. Build the Chroma collection

`02_build_context_db.ipynb` uses these default paths:

```python
md_dir = Path("data/trec_pm2019/trial_markdown")
db_dir = Path("../chroma_context_db")
collection_name = "trec_2019_trials_context"
```

Each chunk contains a title, conditions, interventions and a narrative passage. Passages come from the summary, detailed description or eligibility criteria. Title, conditions and interventions are capped at 60, 60 and 40 tokens. Complete chunks fit within 450 tokens; body overlap is 50 tokens.

The model is `BAAI/bge-small-en-v1.5`, with normalized embeddings and cosine distance. Metadata includes `nct_id`, `source` and `section`. The evaluated collection has 43,459 chunks covering all 8,567 trials. Indexing stops if the collection already exists; use a new collection name to rebuild.

## 4. Build the BM25 index

Run `03_build_bm25.ipynb` in order. Each Markdown file is one document indexed by `BM25Retriever`. Both documents and queries are lowercased and tokenized with `[a-z0-9]+`.

The index is saved to `../bm25/bm25_index.pkl`; the save cell creates its parent directory if needed. It includes term statistics, ordered metadata and tokenization settings. Source text is read from Markdown, so keep the files consistent with the saved index; rebuild the relevant indexes when updating the corpus. The final cells reload the index and run a query. The stored source paths are relative to `examples/chroma_mcp`, which must also be the working directory when reloading in `03` and `04`.

## 5. Run hybrid retrieval

Open `04_hybrid_retrieval.ipynb` and check that the index path, database path, collection and model match the build settings.

Vector retrieval takes 100 chunks and keeps the first hit per NCT ID. BM25 returns 100 trials. `EnsembleRetriever` fuses the two trial rankings using equal weights and RRF `c=60`, then displays the top 10 NCT IDs.

## 6. Connect BioPaster

BioPaster needs a working model-provider configuration. From `examples/chroma_mcp`:

```bash
cp embedding_profiles.example.json embedding_profiles.json
python -c "import sys; print(sys.executable)"
```

Replace the paths in `embedding_profiles.json`:

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

Add the following entry to `mcpServers` in `~/.biopaster/config.json`, keeping other provider and server settings. Replace every absolute path. The Python executable must belong to the environment with the demo dependencies:

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

Restart `biopaster`; it launches the stdio service. [server.py](server.py) exposes:

| Tool | Purpose |
| --- | --- |
| `list_collections` | List Chroma collections |
| `search_documents` | Return hybrid rankings, NCT IDs, matched text and metadata |
| `get_trial` | Read a complete Markdown document by NCT ID |

Try these prompts in order:

> Search trec_2019_trials_context for melanoma BRAF V600E and return three candidate trials.

> Use get_trial to read the first trial. List its eligibility criteria and cite its NCT ID.

Search returns `retrieval_method: hybrid_rrf` and `rank`, not cosine distances as fusion scores. Matched text may be a vector chunk or a BM25 document; use `get_trial` when the complete source is needed.

## 7. Evaluate retrieval

`05_evaluate_retrieval.ipynb` defaults to the data and indexes created above. Its configuration includes:

```python
output_dir = Path("../../docs/assets/trec2019/hybrid")
sample_size = 10
sample_seed = 42
```

Running the export cells writes into the published results directory above. Use a separate `output_dir` when experimenting to preserve the reference results.

The notebook measures P@10, nDCG@10 and Recall@100. To compare Recall@100, it retrieves 100 distinct trials per branch. The service and `04` still use a 100-chunk vector budget, so their candidate depths differ.

[Evaluation report, charts and per-query results](../../docs/trec2019_chroma_retrieval.md).

## Use your own documents

The filenames, section names and `get_trial` implementation in this example are specific to NCT trials. For other documents, adapt background fields and passage extraction to their structure, then use one stable identifier consistently in vector metadata, BM25 metadata, deduplication, RRF's `id_key`, and source-document lookup. Markdown can remain the source format. Rebuild the vector collection and update its profile when changing the embedding model.
