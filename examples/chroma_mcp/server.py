import argparse
from pathlib import Path
import json
from functools import lru_cache
from typing import Annotated

from pydantic import Field
from langchain_huggingface import HuggingFaceEmbeddings
import chromadb
from langchain_chroma import Chroma
from mcp.server.fastmcp import FastMCP
import pickle
import re
from langchain_community.document_loaders import TextLoader
from langchain_community.retrievers import BM25Retriever
from langchain_classic.retrievers import EnsembleRetriever
from langchain_core.runnables import RunnableLambda


@lru_cache(maxsize=4)
def get_embedding_model(
    model_name: str,
    normalize_embeddings: bool,
) -> HuggingFaceEmbeddings:
    return HuggingFaceEmbeddings(
        model_name=model_name,
        model_kwargs={
            "device": "cuda",
            "local_files_only": True,
        },
        encode_kwargs={
            "normalize_embeddings": normalize_embeddings,
            "batch_size": 32,
        },
    )
    
@lru_cache(maxsize=4)
def load_bm25_retriever(
    index_path: str,
    documents_dir: str,
) -> BM25Retriever:
    with Path(index_path).open("rb") as file:
        saved_index = pickle.load(file)

    documents = []

    for metadata in saved_index["metadatas"]:
        path = Path(documents_dir) / f"{metadata['nct_id']}.md"

        document = TextLoader(
            str(path),
            encoding="utf-8",
        ).load()[0]

        document.metadata.update(metadata)
        document.metadata["source"] = str(path)
        documents.append(document)

    return BM25Retriever(
        vectorizer=saved_index["vectorizer"],
        docs=documents,
        preprocess_func=lambda text: re.findall(
            saved_index["token_pattern"],
            text.lower() if saved_index["lowercase"] else text,
        ),
        k=100,
    )

def unique_trials(documents):
    trials = []
    seen = set()

    for document in documents:
        nct_id = document.metadata["nct_id"]
        if nct_id not in seen:
            seen.add(nct_id)
            trials.append(document)

    return trials

def create_server(
    data_directory: str,
    embedding_profiles: dict,
) -> FastMCP:
    database_path = Path(data_directory).expanduser().resolve()

    if not (database_path / "chroma.sqlite3").is_file():
        raise ValueError(
            f"Chroma database not found: {database_path}"
        )

    client = chromadb.PersistentClient(
        path=str(database_path),
    )

    server = FastMCP("chroma")
    vectorstores: dict[str, Chroma] = {}

    @server.tool()
    def list_collections(
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        """List existing Chroma collections with names and metadata."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

        if offset < 0:
            raise ValueError("offset must be non-negative")

        collections = client.list_collections(
            limit=limit,
            offset=offset,
        )

        return {
            "collections": [
                {
                    "name": collection.name,
                    "metadata": collection.metadata,
                }
                for collection in collections
            ],
            "total": client.count_collections(),
            "offset": offset,
        }
        
    @server.tool()
    def search_documents(
        collection_name: Annotated[str, Field(description="Collection to search")],
        query:  Annotated[str, Field(description="Text to search for")],
        top_k: Annotated[int, Field(description="Maximum number of hits", ge=1, le=10)] = 3,
    ) -> dict:
        """Search clinical trials using vector retrieval and BM25 with RRF."""
        if not query.strip():
            raise ValueError("query must not be empty")
        if not 1 <= top_k <= 10:
            raise ValueError("top_k must be between 1 and 10")

        profile = embedding_profiles.get(collection_name)
        if not isinstance(profile, dict):
            raise ValueError(
                f"No embedding profile configured for {collection_name}"
            )

        vectorstore = vectorstores.get(collection_name)
        if vectorstore is None:
            embedding_model = get_embedding_model(
                profile["model_name"],
                profile["normalize_embeddings"],
            )
            vectorstore = Chroma(
                collection_name=collection_name,
                embedding_function=embedding_model,
                client=client,
                create_collection_if_not_exists=False,
            )
            vectorstores[collection_name] = vectorstore

        bm25_retriever = load_bm25_retriever(
            profile["bm25_index"],
            profile["documents_dir"],
        )

        vector_retriever = (
            vectorstore.as_retriever(search_kwargs={"k": 100})
            | RunnableLambda(unique_trials)
        )

        hybrid_retriever = EnsembleRetriever(
            retrievers=[vector_retriever, bm25_retriever],
            weights=[0.5, 0.5],
            c=60,
            id_key="nct_id",
        )

        results = hybrid_retriever.invoke(query)[:top_k]

        return {
            "collection": collection_name,
            "retrieval_method": "hybrid_rrf",
            "hits": [
                {
                    "rank": rank,
                    "id": document.metadata["nct_id"],
                    "text": document.page_content,
                    "metadata": document.metadata,
                }
                for rank, document in enumerate(results, start=1)
            ],
        }
        
    @server.tool()
    def get_trial(
        collection_name: Annotated[
            str, Field(description="Collection containing the trial")
        ],
        nct_id: Annotated[
            str, Field(description="Clinical trial ID, such as NCT01264380")
        ],
    ) -> dict:
        """Read the complete source Markdown for a clinical trial."""
        nct_id = nct_id.strip().upper()
        if not re.fullmatch(r"NCT[0-9]{8}", nct_id):
            raise ValueError("Invalid NCT ID")

        profile = embedding_profiles.get(collection_name)
        if not isinstance(profile, dict):
            raise ValueError(f"No profile configured for {collection_name}")

        documents_dir = Path(profile["documents_dir"]).expanduser().resolve()
        path = documents_dir / f"{nct_id}.md"

        if not path.is_file():
            raise ValueError(f"Trial document not found: {nct_id}")

        return {
            "collection": collection_name,
            "id": nct_id,
            "text": path.read_text(encoding="utf-8"),
            "metadata": {
                "nct_id": nct_id,
                "source": str(path),
            },
        }


    return server



if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--embedding-profiles")
    arguments = parser.parse_args()

    embedding_profiles = {}
    if arguments.embedding_profiles:
        profiles_path = Path(arguments.embedding_profiles).expanduser()
        embedding_profiles = json.loads(
            profiles_path.read_text(encoding="utf-8")
        )
        if not isinstance(embedding_profiles, dict):
            raise ValueError("Embedding profiles must be a JSON object")

    server = create_server(arguments.data_dir, embedding_profiles)
    server.run(transport="stdio")