"""Okapi BM25 keyword search in pure Python: the baseline's retrieval (issue #9).

Vector search joins in week 2 with the Neo4j vector index; this stays as the
keyword half of hybrid retrieval.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

_TOKEN = re.compile(r"[a-z0-9]+")
# Very common English words that carry no meaning for matching.
STOPWORDS = frozenset(
    "a an and are as at be but by can do for from has have i if in is it its me my no not of on or "
    "our so that the their them then there they this to was we what when which who will with you your "
    "hi thanks please just".split()
)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS]


@dataclass(frozen=True)
class Document:
    id: str
    text: str


@dataclass(frozen=True)
class Hit:
    id: str
    score: float


class BM25:
    def __init__(self, documents: list[Document], k1: float = 1.5, b: float = 0.75) -> None:
        if len({d.id for d in documents}) != len(documents):
            raise ValueError("document ids must be unique")
        self.documents = documents
        self.k1, self.b = k1, b
        self._terms = [Counter(tokenize(d.text)) for d in documents]
        self._lengths = [sum(t.values()) for t in self._terms]
        self._avg = (sum(self._lengths) / len(documents)) if documents else 0.0
        df: Counter[str] = Counter()
        for terms in self._terms:
            df.update(terms.keys())
        n = len(documents)
        self._idf = {term: math.log(1 + (n - f + 0.5) / (f + 0.5)) for term, f in df.items()}

    def search(self, query: str, k: int = 5) -> list[Hit]:
        """The top ``k`` documents by BM25 score; documents that match no term are left out."""
        words = tokenize(query)
        hits = []
        for doc, terms, length in zip(self.documents, self._terms, self._lengths, strict=True):
            score = 0.0
            for word in words:
                tf = terms.get(word, 0)
                if not tf:
                    continue
                norm = tf + self.k1 * (1 - self.b + self.b * length / self._avg)
                score += self._idf[word] * tf * (self.k1 + 1) / norm
            if score > 0:
                hits.append(Hit(doc.id, round(score, 4)))
        hits.sort(key=lambda h: (-h.score, h.id))
        return hits[:k]
