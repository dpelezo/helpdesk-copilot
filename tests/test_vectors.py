"""Embeddings, vector search, hybrid ranking and the retrieval eval (#30). No network."""

import hashlib
import json

import httpx
import numpy as np
import pytest

from app import llm
from app.evals import retrieval
from app.retrieval.bm25 import BM25, Document, Hit
from app.retrieval.vectors import MAX_CHARS, RRF_K, CachedEmbedder, Hybrid, VectorIndex, rrf

KEY = "sk-test-not-a-real-key-123"


class BagOfWords:
    """A deterministic stand-in embedder: each word adds to one of 64 dimensions."""

    def __init__(self):
        self.calls = []

    def embed(self, texts, model=None):
        self.calls.append((list(texts), model))
        out = []
        for text in texts:
            v = np.zeros(64)
            for word in text.lower().split():
                v[int(hashlib.md5(word.strip(".,:?!()").encode()).hexdigest(), 16) % 64] += 1
            v[63] += 0.01  # never a zero vector
            out.append(v.tolist())
        return out


def embedder(tmp_path=None, **kw):
    return CachedEmbedder(BagOfWords(), "fake-embed", cache=tmp_path, **kw)


# ── The client's embeddings call ───────────────────────────────────────


def http_client(handler, **kw):
    return llm.ChatClient(model="m", api_key=KEY, transport=httpx.MockTransport(handler), retries=1, **kw)


def test_embed_posts_to_the_embeddings_endpoint_and_keeps_input_order():
    seen = {}

    def handler(request):
        seen["url"], seen["body"] = str(request.url), json.loads(request.content)
        seen["auth"] = request.headers["Authorization"]
        # Returned out of order on purpose: the client sorts by index.
        return httpx.Response(200, json={"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}],
                                         "usage": {"prompt_tokens": 7}})

    c = http_client(handler, embed_model="thenlper/gte-large")
    assert c.embed(["a", "b"]) == [[1.0, 0.0], [0.0, 1.0]]
    assert seen["url"].endswith("/v2/ai/openai/embeddings") and seen["auth"] == f"Bearer {KEY}"
    assert seen["body"] == {"model": "thenlper/gte-large", "input": ["a", "b"]}
    assert (c.calls, c.prompt_tokens, c.completion_tokens) == (1, 7, 0)


def test_embed_needs_a_model_and_respects_the_budget():
    c = http_client(lambda r: httpx.Response(200, json={"data": []}))
    with pytest.raises(llm.LLMError, match="set LLM_EMBED_MODEL"):
        c.embed(["a"])
    spent = http_client(lambda r: httpx.Response(500), embed_model="e", budget=10)
    spent.prompt_tokens = 10
    with pytest.raises(llm.BudgetExceeded):
        spent.embed(["a"])


@pytest.mark.parametrize("body", [{"data": [{"index": 0}]}, {"nope": 1}, {"data": [{"index": 0, "embedding": [1]}] * 2}])
def test_a_malformed_or_mismatched_reply_is_an_error(body):
    c = http_client(lambda r: httpx.Response(200, json=body), embed_model="e")
    with pytest.raises(llm.LLMError, match="embeddings"):
        c.embed(["a"])


def test_an_error_body_that_echoes_the_key_is_redacted():
    c = http_client(lambda r: httpx.Response(401, text=f"bad key {KEY}"), embed_model="e")
    with pytest.raises(llm.LLMError) as error:
        c.embed(["a"])
    assert "HTTP 401" in str(error.value) and "[redacted]" in str(error.value) and KEY not in str(error.value)


def test_from_env_reads_the_embedding_model(monkeypatch):
    monkeypatch.setenv("TELNYX_API_KEY", KEY)
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_EMBED_MODEL", "thenlper/gte-large")
    assert llm.ChatClient.from_env().embed_model == "thenlper/gte-large"


def test_an_empty_list_makes_no_call():
    c = http_client(lambda r: pytest.fail("no call expected"), embed_model="e")
    assert c.embed([]) == []


# ── Cache ──────────────────────────────────────────────────────────────


def test_vectors_are_cached_by_model_and_text(tmp_path):
    first = embedder(tmp_path)
    vectors = first(["vpn fails", "printer offline"])
    second = embedder(tmp_path)
    assert second(["printer offline", "vpn fails"]) == vectors[::-1]
    assert (second.cached, second.embedded, second.client.calls) == (2, 0, [])
    other = CachedEmbedder(BagOfWords(), "another-model", cache=tmp_path)
    other(["vpn fails"])
    assert other.embedded == 1  # a different model never reads another's vectors


def test_texts_are_batched_and_cut_to_the_model_limit():
    e = embedder(batch=2)
    e(["a", "b", "c", "x" * (MAX_CHARS + 100)])
    assert [len(texts) for texts, _ in e.client.calls] == [2, 2]
    assert len(e.client.calls[1][0][1]) == MAX_CHARS
    assert all(model == "fake-embed" for _, model in e.client.calls)


# ── Search and fusion ──────────────────────────────────────────────────


DOCS = [Document("cause:vpn", "VPN certificate expired"), Document("cause:printer", "printer offline jammed"),
        Document("policy:mfa", "MFA reset needs approval")]


def test_vector_search_ranks_by_cosine():
    index = VectorIndex(DOCS, embedder())
    hits = index.search("my printer is offline", 2)
    assert hits[0].id == "cause:printer" and len(hits) == 2 and hits[0].score >= hits[1].score


def test_vector_search_refuses_duplicates_and_zero_vectors():
    with pytest.raises(ValueError, match="unique"):
        VectorIndex([DOCS[0], DOCS[0]], embedder())

    class Zero:
        def __call__(self, texts):
            return [[0.0, 0.0] for _ in texts]

    with pytest.raises(ValueError, match="zero vector"):
        VectorIndex(DOCS, Zero())


def test_rrf_adds_one_over_k_plus_rank():
    fused = rrf([[Hit("a", 9), Hit("b", 8)], [Hit("b", 0.9), Hit("c", 0.8)]], top=3)
    assert [h.id for h in fused] == ["b", "a", "c"]
    assert fused[0].score == round(1 / (RRF_K + 2) + 1 / (RRF_K + 1), 6)
    assert RRF_K == 60  # the paper's constant, fixed before any measurement


def test_rrf_breaks_ties_by_id_so_results_are_stable():
    assert [h.id for h in rrf([[Hit("z", 1)], [Hit("a", 1)]])] == ["a", "z"]


def test_hybrid_finds_what_either_half_finds():
    keyword, vector = BM25(DOCS), VectorIndex(DOCS, embedder())
    hits = Hybrid(keyword, vector).search("certificate", 3)
    assert hits[0].id == "cause:vpn"


# ── The eval ───────────────────────────────────────────────────────────


def test_the_eval_reproduces_the_baselines_bm25_hit_rate():
    """Same tickets, corpus and query: BM25 must give the baseline's 82.6% on held-out."""
    result = retrieval.evaluate(embedder(), "heldout")
    baseline = json.loads(open("evals/results/baseline-bm25-heldout.json").read())
    assert result["hit_at_5"]["bm25"] == baseline["retrieval_hit_at_k"] == 82.6
    assert result["tickets"] == 92 and set(result["hit_at_5"]) == {"bm25", "vector", "hybrid"}
    assert sum(v["tickets"] for v in result["per_issue"].values()) == 92


def test_every_row_records_the_rank_and_top_five_for_each_method():
    result = retrieval.evaluate(embedder(), "tuning")
    row = result["rows"][0]
    for method in retrieval.METHODS:
        assert len(row[method]["top"]) <= 5
        rank = row[method]["rank"]
        assert rank is None or row[method]["top"][rank - 1] == f"cause:{row['cause']}"
    assert result["settings"] == {"top_k": 5, "rrf_k": 60, "hybrid_depth": 20}


def test_the_cli_needs_an_embedding_model(monkeypatch, capsys):
    monkeypatch.setenv("TELNYX_API_KEY", KEY)
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.delenv("LLM_EMBED_MODEL", raising=False)
    assert retrieval.main(["--split", "tuning"]) == 1
    assert "set LLM_EMBED_MODEL" in capsys.readouterr().err


def test_the_summary_lists_every_method_and_issue():
    text = retrieval.summary(retrieval.evaluate(embedder(), "tuning"))
    assert "| bm25 |" in text and "| hybrid |" in text and "| Issue | Tickets |" in text
