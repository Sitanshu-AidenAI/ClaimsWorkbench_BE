"""An in-memory policy vector store.

Implements the same Protocol as the Qdrant one, including the parts that are easy to
get wrong and that a mock would paper over: metadata filtering, cosine ordering, and —
most importantly — **overwrite-on-same-id**, which is the behaviour deterministic point
ids exist to produce. A double that appended instead would make the re-ingest test pass
while the real store accumulated a second copy of every clause.

It knows nothing about the real client's API surface, which is the one thing this
cannot verify. Separate from `tests/fakes/vectors.py` for the reason the real stores are
separate: this Protocol takes no case id, so satisfying the other one would mean
inventing one.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.services.policies.vectors import PolicyVectorHit, PolicyVectorRecord, point_id_for


class FakePolicyVectorStore:
    """Points in a dict, keyed the way the real store keys them."""

    name = "fake-policies"

    def __init__(self, *, fail_on_upsert: bool = False, fail_on_search: bool = False) -> None:
        self.points: dict[uuid.UUID, tuple[list[float], dict[str, object]]] = {}
        self.dimension: int | None = None
        self.upsert_calls = 0
        self.delete_calls = 0
        self.search_calls = 0
        self._fail_on_upsert = fail_on_upsert
        self._fail_on_search = fail_on_search

    async def ensure_ready(self, *, dimension: int) -> None:
        self.dimension = dimension

    async def upsert(self, records: Sequence[PolicyVectorRecord]) -> int:
        self.upsert_calls += 1
        if self._fail_on_upsert:
            raise ConnectionError("The policy vector store is unreachable.")

        for record in records:
            # Assignment, not append. Two upserts of the same passage are one point,
            # which is the whole reason the id is derived from the chunk ref.
            self.points[point_id_for(record.chunk_ref)] = (
                record.vector,
                {
                    "chunk_ref": record.chunk_ref,
                    "policy_document_id": str(record.document_id),
                    "policy_id": str(record.policy_id) if record.policy_id else None,
                    "policy_number": record.policy_number,
                    "line_of_business": record.line_of_business,
                    "content_hash": record.content_hash,
                    "page_number": record.page_number,
                    "section_label": record.section_label,
                },
            )
        return len(records)

    async def delete_for_document(self, document_id: uuid.UUID) -> None:
        self.delete_calls += 1
        doomed = [
            point
            for point, (_, payload) in self.points.items()
            if payload.get("policy_document_id") == str(document_id)
        ]
        for point in doomed:
            del self.points[point]

    async def search(
        self,
        vector: list[float],
        *,
        limit: int,
        document_id: uuid.UUID | None = None,
        policy_id: uuid.UUID | None = None,
        line_of_business: str | None = None,
        document_ids: Sequence[uuid.UUID] | None = None,
    ) -> list[PolicyVectorHit]:
        self.search_calls += 1
        if self._fail_on_search:
            raise ConnectionError("The policy vector store is unreachable.")

        wanted = {str(value) for value in document_ids} if document_ids else None

        scored: list[PolicyVectorHit] = []
        for stored, payload in self.points.values():
            if document_id is not None and payload.get("policy_document_id") != str(document_id):
                continue
            if wanted is not None and payload.get("policy_document_id") not in wanted:
                continue
            if policy_id is not None and payload.get("policy_id") != str(policy_id):
                continue
            if line_of_business and payload.get("line_of_business") != line_of_business:
                continue
            ref = payload.get("chunk_ref")
            raw = payload.get("policy_document_id")
            if not isinstance(ref, str):
                continue
            scored.append(
                PolicyVectorHit(
                    chunk_ref=ref,
                    score=_cosine(vector, stored),
                    document_id=uuid.UUID(raw) if isinstance(raw, str) else None,
                )
            )

        scored.sort(key=lambda hit: hit.score, reverse=True)
        return scored[:limit]

    async def aclose(self) -> None:
        return None


def _cosine(left: list[float], right: list[float]) -> float:
    """Cosine similarity, in pure Python. No numpy for twelve lines of arithmetic."""
    if not left or not right:
        return 0.0
    size = min(len(left), len(right))
    dot = sum(left[index] * right[index] for index in range(size))
    left_norm = sum(value * value for value in left[:size]) ** 0.5
    right_norm = sum(value * value for value in right[:size]) ** 0.5
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


__all__ = ["FakePolicyVectorStore"]
