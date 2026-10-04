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

Use `examples/chroma_mcp` as the notebook working directory. The configuration cells in `02` and `05` contain local execution paths; replace them with your own paths. The database, Markdown corpus and BM25 index are generated locally and are not included in the repository.

## 1. Install dependencies

Use Python 3.12. From the repository root:

```bash
python -m pip install -r examples/chroma_mcp/requirements.txt
python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5', device='cpu')"
```

The second command downloads the embedding model into the local cache. The notebooks and MCP server use CUDA by default. For CPU execution, change their `device` settings to `cpu`; the server setting is in `get_embedding_model()`. Search loads the cached model without downloading it.

## 2. Prepare source documents

From a terminal in `examples/chroma_mcp`:

```bash
mkdir -p data/trec_pm2019 ../bm25
wget -c -P data/trec_pm2019 https://trec.nist.gov/data/precmed/topics2019.xml
wget -c -P data/trec_pm2019 https://trec.nist.gov/data/precmed/qrels-treceval-trials.38.txt
```

Open `01_prepare_trec.ipynb`, run the imports, skip **Download benchmark files**, and continue from **Download trial corpus**. The skipped cell uses `ddata/trec_pm2019`; the commands above put the judgment files in the `data/trec_pm2019` directory expected by subsequent cells.

The four trial archives total approximately 1 GB. The notebook selects 8,567 trials referenced by qrels and creates `trial_markdown/NCTxxxxxxxx.md`. These Markdown files remain the full-text source.

## 3. Build the Chroma collection

Set the paths in `02_build_context_db.ipynb`:

```python
md_dir = Path("data/trec_pm2019/trial_markdown")
db_dir = Path("../chroma_context_db")
collection_name = "trec_2019_trials_context"
```

Each chunk contains a title, conditions, interventions and a narrative passage. Passages come from the summary, detailed description or eligibility criteria. Title, conditions and interventions are capped at 60, 60 and 40 tokens. Complete chunks fit within 450 tokens; body overlap is 50 tokens.

The model is `BAAI/bge-small-en-v1.5`, with normalized embeddings and cosine distance. Metadata includes `nct_id`, `source` and `section`. The evaluated collection has 43,459 chunks covering all 8,567 trials. Indexing stops if the collection already exists; use a new collection name to rebuild.

## 4. Build the BM25 index

Run `03_build_bm25.ipynb` in order. Each Markdown file is one document indexed by `BM25Retriever`. Both documents and queries are lowercased and tokenized with `[a-z0-9]+`.

The index is saved to `../bm25/bm25_index.pkl`, including term statistics, ordered metadata and tokenization settings. Source text is read from Markdown, so keep the files consistent with the saved index; rebuild the relevant indexes when updating the corpus. The final cells reload the index and run a query.

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

Before running `05_evaluate_retrieval.ipynb`, set the data, BM25, Chroma and output paths in its configuration cell. For repository-local result files:

```python
output_dir = Path("../../docs/assets/trec2019/hybrid")
sample_size = 10
sample_seed = 42
```

The notebook measures P@10, nDCG@10 and Recall@100. To compare Recall@100, it retrieves 100 distinct trials per branch. The service and `04` still use a 100-chunk vector budget, so their candidate depths differ.

[Evaluation report, charts and per-query results](../../docs/trec2019_chroma_retrieval.md).

## Use your own documents

The filenames, section names and `get_trial` implementation in this example are specific to NCT trials. For other documents, adapt background fields and passage extraction to their structure, then use one stable identifier consistently in vector metadata, BM25 metadata, deduplication, RRF's `id_key`, and source-document lookup. Markdown can remain the source format. Rebuild the vector collection and update its profile when changing the embedding model.
