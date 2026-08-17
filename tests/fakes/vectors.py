"""An in-memory vector store.

Implements the same Protocol as the Qdrant one, including the parts that are easy to
get wrong and that a mock would paper over: filtering by case *and* document, cosine
ordering, and — most importantly — **overwrite-on-same-id**, which is the behaviour
deterministic point ids exist to produce. A double that appended instead would make the
re-index test pass while the real store accumulated duplicates.

It knows nothing about the real client's API surface, which is the one thing this cannot
verify. That is what the `skipif`-guarded integration test against a real Qdrant is for.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from app.services.intelligence.vectors import VectorHit, VectorRecord, point_id_for


class FakeVectorStore:
    """Points in a dict, keyed the way the real store keys them."""

    name = "fake"

    def __init__(self, *, fail_on_upsert: bool = False) -> None:
        self.points: dict[uuid.UUID, tuple[list[float], dict[str, object]]] = {}
        self.dimension: int | None = None
        self.upsert_calls = 0
        self.delete_calls = 0
        self.search_calls = 0
        self._fail_on_upsert = fail_on_upsert

    async def ensure_ready(self, *, dimension: int) -> None:
        self.dimension = dimension

    async def upsert(self, records: Sequence[VectorRecord]) -> int:
        self.upsert_calls += 1
        if self._fail_on_upsert:
            raise ConnectionError("The vector store is unreachable.")

        for record in records:
            # Assignment, not append. Two upserts of the same passage are one point,
            # which is the whole reason the id is derived from the chunk ref.
            self.points[point_id_for(record.chunk_ref)] = (
                record.vector,
                {
                    "chunk_ref": record.chunk_ref,
                    "fnol_case_id": str(record.case_id),
                    "fnol_document_id": str(record.document_id),
                    "content_hash": record.content_hash,
                    "page_number": record.page_number,
                    "section_label": record.section_label,
                },
            )
        return len(records)

    async def delete_for_document(self, document_id: uuid.UUID) -> None:
        self._delete_where("fnol_document_id", document_id)

    async def delete_for_case(self, case_id: uuid.UUID) -> None:
        self._delete_where("fnol_case_id", case_id)

    def _delete_where(self, key: str, value: uuid.UUID) -> None:
        self.delete_calls += 1
        doomed = [
            point for point, (_, payload) in self.points.items() if payload.get(key) == str(value)
        ]
        for point in doomed:
            del self.points[point]

    async def search(
        self,
        vector: list[float],
        *,
        case_id: uuid.UUID,
        limit: int,
        document_id: uuid.UUID | None = None,
    ) -> list[VectorHit]:
        self.search_calls += 1

        scored: list[VectorHit] = []
        for stored, payload in self.points.values():
            if payload.get("fnol_case_id") != str(case_id):
                continue
            if document_id is not None and payload.get("fnol_document_id") != str(document_id):
                continue
            ref = payload.get("chunk_ref")
            if isinstance(ref, str):
                scored.append(VectorHit(chunk_ref=ref, score=_cosine(vector, stored)))

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


__all__ = ["FakeVectorStore"]
