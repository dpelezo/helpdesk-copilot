"""Vector search and hybrid ranking (issue #30): the second half of retrieval, beside BM25.

- ``CachedEmbedder`` embeds through the one model client and caches every vector by model
  and text, so a re-run pays only for new text.
- ``VectorIndex`` is exact cosine search in memory, for the eval corpus (about 60 documents,
  where an approximate index would add nothing). Published release nodes are searched with
  Neo4j's native vector index instead (``app.knowledge.publish``); both return the same ``Hit``.
- ``rrf`` fuses ranked lists by reciprocal rank (Cormack et al., 2009), so BM25 scores and
  cosine similarities never have to be put on one scale. ``k = 60`` is the paper's constant,
  fixed before any measurement rather than tuned on the eval.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

from app.retrieval.bm25 import BM25, Document, Hit

RRF_K = 60
CACHE = Path("data/cache/embeddings")
# gte-large reads at most 512 tokens and rejects longer input; this many characters stays under it.
MAX_CHARS = 1500


class Embeds(Protocol):
    def embed(self, texts: list[str], model: str | None = None) -> list[list[float]]: ...


@dataclass
class CachedEmbedder:
    client: Embeds
    model: str
    cache: Path | None = CACHE
    batch: int = 32
    cached: int = 0
    embedded: int = 0

    def _path(self, text: str) -> Path | None:
        if self.cache is None:
            return None
        return self.cache / f"{hashlib.sha256(f'{self.model}|{text}'.encode()).hexdigest()}.json"

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        texts = [t[:MAX_CHARS] for t in texts]
        out: dict[int, list[float]] = {}
        todo: list[int] = []
        for i, text in enumerate(texts):
            path = self._path(text)
            if path and path.exists():
                out[i] = json.loads(path.read_text())
                self.cached += 1
            else:
                todo.append(i)
        for start in range(0, len(todo), self.batch):
            chunk = todo[start:start + self.batch]
            vectors = self.client.embed([texts[i] for i in chunk], self.model)
            for i, vector in zip(chunk, vectors, strict=True):
                out[i] = vector
                if path := self._path(texts[i]):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(vector))
            self.embedded += len(chunk)
        return [out[i] for i in range(len(texts))]


def _unit(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    if np.any(norms == 0):
        raise ValueError("a zero vector cannot be compared by cosine")
    return matrix / norms


@dataclass
class VectorIndex:
    """Exact cosine search over documents embedded once."""

    documents: list[Document]
    embed: CachedEmbedder | object
    _matrix: np.ndarray = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if len({d.id for d in self.documents}) != len(self.documents):
            raise ValueError("document ids must be unique")
        vectors = self.embed([d.text for d in self.documents])  # type: ignore[operator]
        self._matrix = _unit(np.asarray(vectors, dtype=float))

    def search(self, query: str, k: int = 5) -> list[Hit]:
        [vector] = self.embed([query])  # type: ignore[operator]
        scores = self._matrix @ _unit(np.asarray(vector, dtype=float))
        order = sorted(range(len(self.documents)), key=lambda i: (-scores[i], self.documents[i].id))
        return [Hit(self.documents[i].id, round(float(scores[i]), 4)) for i in order[:k]]


def rrf(rankings: Sequence[Sequence[Hit]], k: int = RRF_K, top: int = 5) -> list[Hit]:
    """Reciprocal rank fusion: each list adds 1 / (k + rank) to every document it ranks."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            scores[hit.id] = scores.get(hit.id, 0.0) + 1.0 / (k + rank)
    fused = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [Hit(doc_id, round(score, 6)) for doc_id, score in fused[:top]]


@dataclass
class Hybrid:
    """BM25 and vectors, each asked for ``depth`` results, fused by RRF."""

    keyword: BM25
    vector: VectorIndex
    depth: int = 20

    def search(self, query: str, k: int = 5) -> list[Hit]:
        return rrf([self.keyword.search(query, self.depth), self.vector.search(query, self.depth)], top=k)
