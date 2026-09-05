"""Local multilingual embeddings for Learning retrieval.

The model is downloaded/cached by FastEmbed and never receives book text over an
external API.  SQLite stores normalized float32 vectors so a PDF is embedded once.
"""
from __future__ import annotations

import logging
import math
import os
import threading
from array import array
from functools import lru_cache

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
_INFERENCE_LOCK = threading.Lock()


def model_name() -> str:
    return os.getenv("LEARNING_EMBEDDING_MODEL", DEFAULT_MODEL)


@lru_cache(maxsize=1)
def _model():
    from fastembed import TextEmbedding

    cache_dir = os.getenv("LEARNING_EMBEDDING_CACHE", "data/fastembed-cache")
    os.makedirs(cache_dir, exist_ok=True)
    return TextEmbedding(
        model_name=model_name(),
        cache_dir=cache_dir,
        threads=max(1, int(os.getenv("LEARNING_EMBEDDING_THREADS", "2"))),
    )


def enabled() -> bool:
    return os.getenv("LEARNING_SEMANTIC_ENABLED", "1").strip().casefold() not in {
        "0", "false", "no", "off",
    }


def embed_passages(texts: list[str]) -> list[bytes]:
    if not texts or not enabled():
        return []
    with _INFERENCE_LOCK:
        vectors = list(_model().embed(texts, batch_size=16))
    return [array("f", (float(value) for value in vector)).tobytes() for vector in vectors]


def embed_query(text: str) -> bytes | None:
    if not text.strip() or not enabled():
        return None
    with _INFERENCE_LOCK:
        vector = next(iter(_model().query_embed(text)))
    return array("f", (float(value) for value in vector)).tobytes()


def cosine(left: bytes, right: bytes) -> float:
    a, b = array("f"), array("f")
    a.frombytes(left)
    b.frombytes(right)
    if not a or len(a) != len(b):
        return -1.0
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return dot / norm if norm else -1.0
