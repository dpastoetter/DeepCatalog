"""Chunking, embedding, and Chroma-backed retrieval for RAG."""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

import httpx

from deepcatalog import config
from deepcatalog.chroma_local import embedded_chroma_client
from deepcatalog.config import (
    CHROMA_DIR,
    CHUNK_OVERLAP_CHARS,
    CHUNK_SIZE_CHARS,
    RETRIEVE_TOP_K,
    ensure_data_dirs,
)
from deepcatalog.ollama_setup import (
    ensure_ollama_ready,
    format_http_error,
    resolve_runtime_model,
)
from deepcatalog.ollama_url import ollama_client, trusted_ollama_origin
from deepcatalog.tools.metadata_db import (
    clear_all_indexed_at,
    get_document,
    list_all_documents,
    mark_indexed,
)
from deepcatalog.usage import (
    normalize_gemini_usage,
    normalize_openai_usage,
    record_usage,
)

logger = logging.getLogger(__name__)

# Bump when chunking / metadata layout for vectors changes incompatibly.
INDEX_SCHEMA_VERSION = 3
COLLECTION_NAME = "deepcatalog_chunks"
_LEGACY_COLLECTION_NAMES = ("paperless_chunks",)
ONNX_MODEL_NAME = "all-MiniLM-L6-v2"
ONNX_DIMENSION = 384
# Known dims for common models (used in fingerprints before first embed).
_KNOWN_DIMENSIONS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "text-embedding-004": 768,
    "nomic-embed-text": 768,
    "nomic-embed-text:latest": 768,
    "mxbai-embed-large": 1024,
    "mxbai-embed-large:latest": 1024,
    "bge-m3": 1024,
    "bge-m3:latest": 1024,
    "all-minilm": 384,
    "all-minilm:latest": 384,
    ONNX_MODEL_NAME: ONNX_DIMENSION,
}

_index_lock = threading.RLock()
_rebuilding = False
_onnx_ef = None


def _meta_path() -> Path:
    return Path(CHROMA_DIR) / "index_meta.json"


def _load_index_meta() -> dict[str, Any] | None:
    path = _meta_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_index_meta(meta: dict[str, Any]) -> None:
    ensure_data_dirs()
    path = _meta_path()
    path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")


_CTRL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")


def _sanitize_log_text(value: str, *, max_len: int = 200) -> str:
    cleaned = _CTRL_CHARS.sub(" ", value).strip()
    return cleaned[:max_len] or "unspecified"


def mark_index_stale(reason: str = "embedding configuration changed") -> None:
    """Flag the vector index so the next index/retrieve rebuilds it."""
    meta = _load_index_meta() or {}
    meta["stale"] = True
    meta["stale_reason"] = reason
    _write_index_meta(meta)
    logger.info("RAG index marked stale: %s", _sanitize_log_text(reason))


def resolve_embedding_backend() -> dict[str, Any]:
    """
    Describe the embedding backend that embed_texts() will use right now.

    Returned keys: embedding_provider, model, dimension (may be None if unknown),
    schema_version.
    """
    schema = INDEX_SCHEMA_VERSION
    if config.LLM_PROVIDER == "ollama":
        model = resolve_runtime_model(config.EMBEDDING_MODEL) or config.EMBEDDING_MODEL
        return {
            "embedding_provider": "ollama",
            "model": model,
            "dimension": _KNOWN_DIMENSIONS.get(model) or _KNOWN_DIMENSIONS.get(model.split(":")[0]),
            "schema_version": schema,
        }

    if config.LLM_PROVIDER == "openai":
        from deepcatalog.auth import resolve_auth_mode, resolve_openai_api_key

        if resolve_auth_mode() == "api_key" and resolve_openai_api_key():
            model = config.EMBEDDING_MODEL
            return {
                "embedding_provider": "openai",
                "model": model,
                "dimension": _KNOWN_DIMENSIONS.get(model),
                "schema_version": schema,
            }
        # ChatGPT OAuth / no Platform key: genuine local embeddings.
        return _resolve_local_semantic_backend(schema)

    if config.LLM_PROVIDER == "gemini":
        model = config.EMBEDDING_MODEL
        return {
            "embedding_provider": "gemini",
            "model": model,
            "dimension": _KNOWN_DIMENSIONS.get(model),
            "schema_version": schema,
        }

    return _resolve_local_semantic_backend(schema)


def _resolve_local_semantic_backend(schema: int) -> dict[str, Any]:
    """
    On-disk ONNX MiniLM for ChatGPT OAuth / no Platform API key.

    Stable fingerprint (does not depend on whether Ollama happens to be up).
    Ollama users already embed via the ollama provider path with nomic-embed-text.
    """
    return {
        "embedding_provider": "local-onnx",
        "model": ONNX_MODEL_NAME,
        "dimension": ONNX_DIMENSION,
        "schema_version": schema,
    }


def _fingerprint(meta: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(meta.get("embedding_provider") or ""),
        str(meta.get("model") or ""),
        int(meta.get("schema_version") or 0),
        int(meta["dimension"]) if meta.get("dimension") is not None else None,
    )


def _index_is_compatible(stored: dict[str, Any] | None, desired: dict[str, Any]) -> bool:
    if not stored or stored.get("stale"):
        return False
    # Dimension: if either side lacks it, compare provider/model/schema only.
    sp, sm, ss, sd = _fingerprint(stored)
    dp, dm, ds, dd = _fingerprint(desired)
    if (sp, sm, ss) != (dp, dm, ds):
        return False
    if sd is not None and dd is not None and sd != dd:
        return False
    return True


def _chroma_client():
    ensure_data_dirs()
    return embedded_chroma_client(CHROMA_DIR)


def _chroma_collection():
    client = _chroma_client()
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


def _reset_chroma_collection():
    client = _chroma_client()
    for name in (COLLECTION_NAME, *_LEGACY_COLLECTION_NAMES):
        try:
            client.delete_collection(name)
        except Exception:  # noqa: BLE001 — collection may not exist yet
            pass
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


def _document_index_text(document: dict[str, Any]) -> str:
    extracted = document.get("extracted")
    if isinstance(extracted, dict):
        full_text = extracted.get("full_text")
        if isinstance(full_text, str) and full_text.strip():
            return full_text
    raw = document.get("extracted_json")
    if isinstance(raw, str) and raw.strip():
        try:
            payload = json.loads(raw)
            full_text = payload.get("full_text")
            if isinstance(full_text, str) and full_text.strip():
                return full_text
        except json.JSONDecodeError:
            pass
    parts = [
        document.get("summary"),
        document.get("subject"),
        document.get("counterparties"),
        document.get("filename"),
        document.get("doc_type"),
    ]
    return " ".join(str(p) for p in parts if p)


def rebuild_index(*, reason: str = "embedding configuration changed") -> dict[str, Any]:
    """Drop the Chroma collection and re-embed every archived document."""
    global _rebuilding
    with _index_lock:
        _rebuilding = True
        try:
            logger.warning("Rebuilding RAG index (%s)", reason)
            _reset_chroma_collection()
            clear_all_indexed_at()
            desired = resolve_embedding_backend()
            docs = list_all_documents()
            ok = 0
            errors: list[str] = []
            dim: int | None = desired.get("dimension")
            for doc in docs:
                if not doc:
                    continue
                doc_id = doc.get("id")
                if not doc_id:
                    continue
                result = index_document(
                    document_id=str(doc_id),
                    text=_document_index_text(doc),
                    filename=doc.get("filename"),
                    doc_type=doc.get("doc_type"),
                    _skip_compat_check=True,
                )
                if result.get("status") == "success":
                    ok += 1
                    if dim is None and result.get("dimension"):
                        dim = int(result["dimension"])
                else:
                    errors.append(f"{doc_id}: {result.get('error')}")
            meta = {
                "embedding_provider": desired["embedding_provider"],
                "model": desired["model"],
                "dimension": dim,
                "schema_version": INDEX_SCHEMA_VERSION,
                "stale": False,
                "document_count": ok,
                "rebuild_reason": reason,
            }
            _write_index_meta(meta)
            return {
                "status": "success" if not errors else "partial",
                "indexed": ok,
                "errors": errors,
                "meta": meta,
            }
        finally:
            _rebuilding = False


def ensure_index_compatible() -> dict[str, Any] | None:
    """
    If stored index fingerprint differs from the active embedding backend,
    mark stale and rebuild automatically. Returns rebuild result or None.
    """
    global _rebuilding
    if _rebuilding:
        return None
    with _index_lock:
        if _rebuilding:
            return None
        desired = resolve_embedding_backend()
        stored = _load_index_meta()
        if _index_is_compatible(stored, desired):
            return None
        reason = "missing index metadata"
        if stored:
            if stored.get("stale"):
                reason = str(stored.get("stale_reason") or "index marked stale")
            else:
                reason = (
                    "embedding backend changed "
                    f"({stored.get('embedding_provider')}/{stored.get('model')} → "
                    f"{desired.get('embedding_provider')}/{desired.get('model')})"
                )
        return rebuild_index(reason=reason)


def get_index_status() -> dict[str, Any]:
    """Public status for Settings / debugging."""
    desired = resolve_embedding_backend()
    stored = _load_index_meta()
    compatible = _index_is_compatible(stored, desired)
    return {
        "status": "success",
        "compatible": compatible,
        "stale": bool(stored and stored.get("stale")) or not compatible,
        "active": desired,
        "stored": stored,
        "schema_version": INDEX_SCHEMA_VERSION,
    }


def _embed_gemini(texts: list[str]) -> list[list[float]]:
    from google import genai

    if not config.GOOGLE_API_KEY:
        raise RuntimeError("GOOGLE_API_KEY is not set")
    client = genai.Client(api_key=config.GOOGLE_API_KEY)
    vectors: list[list[float]] = []
    for text in texts:
        response = client.models.embed_content(
            model=config.EMBEDDING_MODEL,
            contents=text,
        )
        metadata = getattr(response, "usage_metadata", None)
        if metadata is not None:
            prompt, completion, total = normalize_gemini_usage(metadata)
            record_usage(
                "gemini",
                config.EMBEDDING_MODEL,
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                kind="embed",
            )
        else:
            record_usage("gemini", config.EMBEDDING_MODEL, kind="embed")
        embeddings = getattr(response, "embeddings", None)
        if embeddings:
            vectors.append(list(embeddings[0].values))
            continue
        embedding = getattr(response, "embedding", None)
        if embedding is not None:
            vectors.append(list(embedding.values))
            continue
        raise RuntimeError("Unexpected embedding response shape from Gemini API")
    return vectors


def _embed_openai(texts: list[str]) -> list[list[float]]:
    from openai import OpenAI

    from deepcatalog.auth import resolve_auth_mode, resolve_openai_api_key

    # Platform embeddings need an API key. ChatGPT OAuth uses local semantic embeds.
    if resolve_auth_mode() != "api_key" or not resolve_openai_api_key():
        return _embed_local_semantic(texts)

    from deepcatalog.auth import ensure_openai_env

    ensure_openai_env()
    client = OpenAI()
    response = client.embeddings.create(model=config.EMBEDDING_MODEL, input=texts)
    prompt, completion, total = normalize_openai_usage(getattr(response, "usage", None))
    record_usage(
        "openai",
        config.EMBEDDING_MODEL,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        kind="embed",
    )
    ordered = sorted(response.data, key=lambda item: item.index)
    return [list(item.embedding) for item in ordered]


def _embed_ollama(texts: list[str], *, model: str | None = None) -> list[list[float]]:
    """Embeddings via an Ollama server (/api/embed, batch input)."""
    ensure_ollama_ready()
    resolved = resolve_runtime_model(model or config.EMBEDDING_MODEL)
    origin = trusted_ollama_origin(config.OLLAMA_BASE_URL)
    try:
        with ollama_client(origin=origin, timeout=120) as client:
            resp = client.post(
                "/api/embed",
                json={"model": resolved, "input": texts},
            )
            resp.raise_for_status()
    except httpx.ConnectError as exc:
        raise RuntimeError("Cannot reach Ollama — is `ollama serve` running?") from exc
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(format_http_error(exc, model=resolved, kind="embedding model")) from exc
    payload = resp.json()
    embeddings = payload.get("embeddings")
    if not embeddings or len(embeddings) != len(texts):
        raise RuntimeError("Unexpected embedding response shape from Ollama")
    record_usage("ollama", resolved, kind="embed")
    return [list(vec) for vec in embeddings]


def _get_onnx_embedding_function():
    global _onnx_ef
    if _onnx_ef is None:
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

        _onnx_ef = ONNXMiniLM_L6_V2()
    return _onnx_ef


def _embed_onnx(texts: list[str]) -> list[list[float]]:
    """Genuine local semantic embeddings (all-MiniLM-L6-v2 via Chroma ONNX)."""
    ef = _get_onnx_embedding_function()
    raw = ef(texts)
    vectors: list[list[float]] = []
    for vec in raw:
        vectors.append([float(x) for x in vec])
    record_usage("local-onnx", ONNX_MODEL_NAME, kind="embed")
    return vectors


def _embed_local_semantic(texts: list[str]) -> list[list[float]]:
    """
    Local semantic embeddings for ChatGPT OAuth / no Platform API key.

    Uses on-disk ONNX all-MiniLM-L6-v2 (via Chroma) — a real sentence embedding
    model, not the old UTF-8 byte accumulator.
    """
    return _embed_onnx(texts)


def chunk_text(
    text: str,
    chunk_size: int = CHUNK_SIZE_CHARS,
    overlap: int = CHUNK_OVERLAP_CHARS,
) -> list[str]:
    """Split text into overlapping character chunks, preferring paragraph breaks."""
    cleaned = re.sub(r"\s+\n", "\n", (text or "").strip())
    if not cleaned:
        return []
    if len(cleaned) <= chunk_size:
        return [cleaned]

    chunks: list[str] = []
    start = 0
    length = len(cleaned)
    while start < length:
        end = min(start + chunk_size, length)
        if end < length:
            window = cleaned[start:end]
            split_at = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("\n"))
            if split_at > chunk_size // 3:
                end = start + split_at + 1
        piece = cleaned[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= length:
            break
        start = max(0, end - overlap)
    return chunks


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts with the configured provider (Gemini, OpenAI, or Ollama)."""
    if not texts:
        return []
    if config.LLM_PROVIDER == "openai":
        return _embed_openai(texts)
    if config.LLM_PROVIDER == "ollama":
        return _embed_ollama(texts)
    return _embed_gemini(texts)


def index_document(
    document_id: str,
    text: str,
    filename: str | None = None,
    doc_type: str | None = None,
    *,
    _skip_compat_check: bool = False,
) -> dict[str, Any]:
    """
    Chunk document text, embed chunks, and upsert into the local Chroma store.
    """
    if not _skip_compat_check:
        ensure_index_compatible()

    meta = get_document(document_id)
    if meta.get("status") != "success":
        return meta

    document = meta["document"]
    filename = filename or document.get("filename") or "document"
    doc_type = doc_type or document.get("doc_type") or "other"
    chunks = chunk_text(text or document.get("summary") or "")
    if not chunks:
        fallback = " ".join(
            filter(
                None,
                [
                    document.get("summary"),
                    document.get("counterparties"),
                    filename,
                    doc_type,
                ],
            )
        )
        chunks = chunk_text(fallback) or [f"{filename} ({doc_type})"]

    try:
        vectors = embed_texts(chunks)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"embedding failed: {exc}"}

    if not vectors:
        return {"status": "error", "error": "embedding failed: empty vectors"}

    dimension = len(vectors[0])
    collection = _chroma_collection()
    ids = [f"{document_id}::chunk::{i}" for i in range(len(chunks))]
    metadatas = [
        {
            "document_id": document_id,
            "filename": filename,
            "doc_type": doc_type,
            "chunk_index": i,
        }
        for i in range(len(chunks))
    ]

    existing = collection.get(where={"document_id": document_id})
    if existing and existing.get("ids"):
        collection.delete(ids=existing["ids"])

    collection.upsert(
        ids=ids,
        documents=chunks,
        embeddings=vectors,
        metadatas=metadatas,
    )
    mark_indexed(document_id)

    if not _skip_compat_check and not _rebuilding:
        desired = resolve_embedding_backend()
        _write_index_meta(
            {
                "embedding_provider": desired["embedding_provider"],
                "model": desired["model"],
                "dimension": dimension,
                "schema_version": INDEX_SCHEMA_VERSION,
                "stale": False,
            }
        )

    return {
        "status": "success",
        "document_id": document_id,
        "chunk_count": len(chunks),
        "dimension": dimension,
    }


def retrieve_chunks(
    query: str,
    top_k: int = RETRIEVE_TOP_K,
    doc_type: str | None = None,
) -> dict[str, Any]:
    """Semantic retrieval over indexed document chunks."""
    if not query or not query.strip():
        return {"status": "error", "error": "query must not be empty"}

    ensure_index_compatible()

    try:
        query_vec = embed_texts([query.strip()])[0]
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"embedding failed: {exc}"}

    collection = _chroma_collection()
    kwargs: dict[str, Any] = {
        "query_embeddings": [query_vec],
        "n_results": max(1, min(top_k, 20)),
        "include": ["documents", "metadatas", "distances"],
    }
    if doc_type:
        kwargs["where"] = {"doc_type": doc_type}

    try:
        result = collection.query(**kwargs)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"chroma query failed: {exc}"}

    documents = (result.get("documents") or [[]])[0]
    metadatas = (result.get("metadatas") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    ids = (result.get("ids") or [[]])[0]

    chunks = []
    for i, doc in enumerate(documents):
        meta = metadatas[i] if i < len(metadatas) else {}
        distance = distances[i] if i < len(distances) else None
        chunks.append(
            {
                "chunk_id": ids[i] if i < len(ids) else None,
                "text": doc,
                "document_id": meta.get("document_id"),
                "filename": meta.get("filename"),
                "doc_type": meta.get("doc_type"),
                "chunk_index": meta.get("chunk_index"),
                "distance": distance,
            }
        )

    return {"status": "success", "count": len(chunks), "chunks": chunks}
