"""Elasticsearch job index and hybrid matching.

Every job is indexed once (BM25 text fields + a 384-d dense vector). A profile's candidates
are ranked against its CV with two retrievers, lexical (BM25) and semantic (kNN), fused
with Reciprocal Rank Fusion on the client. Client-side RRF keeps this license-agnostic and
makes the fusion explicit and unit-testable.

Embeddings run locally (fastembed, BAAI/bge-small-en-v1.5, ONNX on CPU): no API quota, and
a visitor's CV never leaves the platform to be embedded.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Sequence
from functools import cached_property
from typing import Any

from elasticsearch import Elasticsearch, helpers

INDEX = "jobs-v1"
DIMS = 384
RRF_K = 60
EMBED_CHARS = 1500  # ~350 tokens: title, company and the head of the description

MAPPING: dict[str, Any] = {
    "properties": {
        "uid": {"type": "keyword"},
        "company": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
        "title": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
        "location": {"type": "text"},
        "description": {"type": "text"},
        "tags": {"type": "keyword"},
        "remote": {"type": "boolean"},
        "posted_at": {"type": "date"},
        "embedding": {
            "type": "dense_vector",
            "dims": DIMS,
            "index": True,
            "similarity": "cosine",
        },
    }
}

Embedder = Callable[[Sequence[str]], list[list[float]]]


def job_text(job: dict[str, Any]) -> str:
    return f"{job['title']} at {job['company']} ({job['location']})\n{job['description']}"[
        :EMBED_CHARS
    ]


class LocalEmbedder:
    """fastembed model, loaded lazily (about 130 MB, cached under FASTEMBED_CACHE_PATH)."""

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5") -> None:
        self.model_name = model

    @cached_property
    def model(self) -> Any:
        from fastembed import TextEmbedding

        return TextEmbedding(self.model_name, cache_dir=os.getenv("FASTEMBED_CACHE_PATH"))

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        return [v.tolist() for v in self.model.embed(list(texts), batch_size=32)]


def rrf(rankings: Iterable[Sequence[str]], k: int = RRF_K) -> dict[str, float]:
    """Reciprocal Rank Fusion: score(d) = sum over rankings of 1 / (k + rank(d))."""
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, uid in enumerate(ranking, 1):
            fused[uid] = fused.get(uid, 0.0) + 1.0 / (k + rank)
    return fused


class JobIndex:
    def __init__(self, es: Elasticsearch, embed: Embedder) -> None:
        self.es = es
        self.embed = embed

    @classmethod
    def from_env(cls) -> JobIndex:
        es = Elasticsearch(os.getenv("ELASTICSEARCH_URL", "http://localhost:9200"))
        return cls(es, LocalEmbedder())

    def ensure_index(self) -> None:
        if not self.es.indices.exists(index=INDEX):
            self.es.indices.create(index=INDEX, mappings=MAPPING)

    def index(self, jobs: list[dict[str, Any]]) -> int:
        if not jobs:
            return 0
        self.ensure_index()
        vectors = self.embed([job_text(j) for j in jobs])
        actions = (
            {
                "_index": INDEX,
                "_id": j["uid"],
                "_source": {
                    "uid": j["uid"],
                    "company": j["company"],
                    "title": j["title"],
                    "location": j["location"],
                    "description": j["description"],
                    "tags": j["tags"],
                    "remote": j["remote"],
                    "posted_at": j["posted_at"],
                    "embedding": v,
                },
            }
            for j, v in zip(jobs, vectors, strict=True)
        )
        ok, _ = helpers.bulk(self.es, actions, refresh="wait_for")
        return int(ok)

    def match(
        self, query_text: str, cv: str, candidate_uids: list[str], top_k: int
    ) -> list[tuple[str, float]]:
        """Rank candidates by fused BM25 + kNN relevance to the profile. Returns the top_k
        (uid, rrf_score), best first."""
        if not candidate_uids:
            return []
        self.ensure_index()
        only = {"terms": {"uid": candidate_uids}}
        depth = min(len(candidate_uids), max(top_k * 3, 50))

        lexical = self.es.search(
            index=INDEX,
            size=depth,
            source=False,
            query={
                "bool": {
                    "filter": [only],
                    "should": [
                        {"multi_match": {"query": query_text, "fields": ["title^4", "tags^2"]}},
                        {"match": {"description": {"query": cv[:4000]}}},
                    ],
                }
            },
        )
        vector = self.embed([cv[:EMBED_CHARS]])[0]
        semantic = self.es.search(
            index=INDEX,
            size=depth,
            source=False,
            knn={
                "field": "embedding",
                "query_vector": vector,
                "k": depth,
                "num_candidates": min(10_000, max(depth * 4, 100)),
                "filter": only,
            },
        )
        fused = rrf([_ids(lexical), _ids(semantic)])
        return sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:top_k]


def _ids(resp: Any) -> list[str]:
    return [h["_id"] for h in resp["hits"]["hits"]]
