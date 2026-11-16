"""RAG ingestion and retrieval for the Acme support knowledge base."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import chromadb
from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_ollama import OllamaEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

BASE_DIR = Path(__file__).resolve().parent
# Both directories default to backend/; Docker overrides them to the mounted
# /app/data and /app/chroma_db volumes.
KB_DIR = Path(os.getenv("KB_DIR", str(BASE_DIR / "data" / "kb")))
CHROMA_DIR = Path(os.getenv("CHROMA_DIR", str(BASE_DIR / "chroma_db")))

# Load backend/.env so the app behaves the same regardless of CWD.
load_dotenv(BASE_DIR / ".env")

COLLECTION_NAME = "acme_kb"
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
DEFAULT_K = 4

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")


def _load_documents() -> list[Document]:
    """Read every .md/.txt file under data/kb/ and return it as a Document."""
    documents: list[Document] = []
    for path in sorted(KB_DIR.rglob("*")):
        if path.suffix.lower() not in (".md", ".txt"):
            continue
        text = path.read_text(encoding="utf-8")
        source = str(path.relative_to(KB_DIR))
        documents.append(
            Document(
                page_content=text,
                metadata={
                    "source": source,
                    "title": source,
                    "source_type": path.suffix.lower().lstrip("."),
                },
            )
        )
    return documents


def _get_embeddings() -> OllamaEmbeddings:
    return OllamaEmbeddings(model=EMBED_MODEL, base_url=OLLAMA_BASE_URL)


def _get_chroma(embeddings: OllamaEmbeddings) -> Chroma:
    return Chroma(
        collection_name=COLLECTION_NAME,
        persist_directory=str(CHROMA_DIR),
        embedding_function=embeddings,
    )


def _reset_collection() -> None:
    """Delete the existing collection so repeated ingest() calls do not create duplicates."""
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    existing_names = [c.name for c in client.list_collections()]
    if COLLECTION_NAME in existing_names:
        client.delete_collection(COLLECTION_NAME)


def _get_collection():
    """Return the raw Chroma collection, creating it if it does not exist."""
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return client.get_or_create_collection(COLLECTION_NAME)



def ingest() -> int:
    """Split all KB documents, embed them, and store them in Chroma."""
    documents = _load_documents()
    if not documents:
        print(f"No .md/.txt files found under {KB_DIR}")
        return 0

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
    )
    chunks = splitter.split_documents(documents)

    embeddings = _get_embeddings()
    _reset_collection()
    vectorstore = _get_chroma(embeddings)
    vectorstore.add_documents(chunks)

    print(
        f"Ingested {len(documents)} document(s) as {len(chunks)} chunk(s) "
        f"into {CHROMA_DIR} (collection: {COLLECTION_NAME})."
    )
    return len(chunks)


def add_document(
    doc_id: str,
    title: str,
    items: list[tuple[str, dict]],
    source_type: str | None = None,
) -> int:
    """Split ``items`` into chunks, embed them, and store them in Chroma.

    Args:
        doc_id: Stable id stored in every chunk's metadata (``doc_id``).
        title: Human-readable document title; also the retrieval source name.
        items: ``(text, metadata)`` tuples from :mod:`loaders`. Metadata may
            contain ``page`` (PDF) or ``csv_qa`` (keep the row as one chunk).
        source_type: One of pdf/csv/docx/txt/md/typed. Falls back to each
            item's ``source_type`` metadata and finally to ``"txt"``.

    Returns:
        The number of chunks written to Chroma.

    Raises:
        ValueError: If none of the items contain indexable text.
    """
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
    )

    chunks: list[Document] = []
    for text, metadata in items:
        clean = (text or "").strip()
        if not clean:
            continue

        meta = dict(metadata or {})
        item_source_type = source_type or meta.get("source_type") or "txt"
        base_metadata: dict = {
            "doc_id": str(doc_id),
            "title": title,
            "source": title,
            "source_type": item_source_type,
        }
        if meta.get("page") is not None:
            base_metadata["page"] = int(meta["page"])
        if meta.get("row") is not None:
            base_metadata["row"] = int(meta["row"])

        if meta.get("csv_qa"):
            # One CSV question/answer row must stay a single chunk.
            chunks.append(Document(page_content=clean, metadata=base_metadata))
        else:
            chunks.extend(
                splitter.create_documents([clean], metadatas=[base_metadata])
            )

    if not chunks:
        raise ValueError("No indexable text was found in this document.")

    embeddings = _get_embeddings()
    vectorstore = _get_chroma(embeddings)
    try:
        vectorstore.add_documents(chunks)
    except Exception:
        # Do not leave a half-indexed document behind if embedding fails.
        delete_document(str(doc_id))
        raise

    return len(chunks)


def delete_document(doc_id: str) -> int:
    """Remove every chunk tagged with ``doc_id`` from Chroma.

    Returns:
        The number of chunks that were deleted.
    """
    collection = _get_collection()
    existing = collection.get(where={"doc_id": str(doc_id)}, include=[])
    ids = existing.get("ids") or []
    if ids:
        collection.delete(ids=ids)
    return len(ids)


def retrieve(query: str, k: int = DEFAULT_K) -> list[dict]:
    """Return the k most relevant chunks, including text, source name, and score.

    The ``source`` value is the document title so callers (and the ticket
    dashboard) can show human-readable citations. ``doc_id``, ``source_type``,
    and ``page`` are included when present.

    Note: Chroma uses L2 distance by default, so a lower score means more relevant.
    """
    embeddings = _get_embeddings()
    vectorstore = _get_chroma(embeddings)
    results = vectorstore.similarity_search_with_score(query, k=k)
    hits: list[dict] = []
    for doc, score in results:
        title = doc.metadata.get("title") or doc.metadata.get("source") or "unknown"
        hit = {
            "text": doc.page_content,
            "source": title,
            "title": title,
            "doc_id": doc.metadata.get("doc_id"),
            "source_type": doc.metadata.get("source_type"),
            "score": score,
        }
        if doc.metadata.get("page") is not None:
            hit["page"] = doc.metadata["page"]
        hits.append(hit)
    return hits


def add_resolution(ticket: str, answer: str) -> dict:
    """Save a resolved ticket to data/kb/ and add it to the vector store."""
    now = datetime.now(timezone.utc)
    filename = f"resolved-ticket-{now.strftime('%Y%m%d-%H%M%S')}.md"

    content = (
        f"# Resolved Ticket\n\n"
        f"**Status:** Resolved\n"
        f"**Date:** {now.strftime('%Y-%m-%d')}\n\n"
        f"## Customer Question\n\n{ticket.strip()}\n\n"
        f"## Final Answer\n\n{answer.strip()}\n"
    )

    KB_DIR.mkdir(parents=True, exist_ok=True)
    filepath = KB_DIR / filename
    filepath.write_text(content, encoding="utf-8")

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
    )
    doc = Document(
        page_content=content,
        metadata={
            "source": filename,
            "title": filename,
            "source_type": "md",
        },
    )
    chunks = splitter.split_documents([doc])

    embeddings = _get_embeddings()
    vectorstore = _get_chroma(embeddings)
    vectorstore.add_documents(chunks)

    print(f"Saved {filepath} and added {len(chunks)} chunk(s) to the vector store.")
    return {"filename": filename, "chunks": len(chunks)}


if __name__ == "__main__":
    ingest()