"""The vector index.

Behind a Protocol, because the store is the most replaceable part of this pipeline —
Qdrant today, pgvector or a managed service tomorrow — and because a test needs an
in-memory one that behaves the same way.

**Postgres is the record; this is a cache.** The payload here holds identifiers and
filter keys and no passage text. That is a deliberate departure from the IIF pipeline,
which stores the full chunk text in the Qdrant payload *and* duplicates it into a
`chunk_fulltext` table — two copies with no owner, which its own retrieval code
acknowledges can desync. Three reasons for the departure:

1. A citation shown to a claims officer is part of the claim record. It has to be
   readable when the vector store is unreachable, and inside the same transaction as
   the field row it justifies.
2. Re-embedding to a new model becomes "read Postgres, write Qdrant" — no re-reading
   of documents, no re-chunking, and the offsets stay identical so citations already
   given out still resolve.
3. A fourth copy of claim material, in a second datastore with its own backup,
   retention and access-control story, is a data-protection cost that buys no read.

The cost is one extra local query per search to hydrate the hits, which is
microseconds against a network round trip to the store itself.

**Point ids are deterministic.** `uuid5` of the passage's ref, so re-indexing
overwrites in place. IIF uses `uuid.uuid4()` here, which means every re-ingestion of a
document adds a second copy of every passage to the collection — the old points can
never be named again, so nothing can remove them. That is the bug this one line fixes.

Two things deliberately *not* in the id:

* **Not the content hash.** Two documents containing the same paragraph are two
  citations. A content-keyed point would let the second upsert overwrite the first's
  payload, and the officer would be shown page 4 of the wrong file.
* **Not the embedding model.** Including it would orphan every old point on a model
  change instead of overwriting it. Excluding it makes switching models a clean
  overwrite, and the drift is caught by the index fingerprint either way.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from app.core.config import DocumentIntelligenceSettings, settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: The namespace for deterministic point ids. Fixed forever: changing it orphans
#: every point in every collection.
POINT_NAMESPACE = uuid.UUID("9a1f2c7e-6b40-4c1d-9f83-2e5a7d0c4b11")

#: Points per upsert call.
UPSERT_BATCH_SIZE = 100


def point_id_for(chunk_ref: str) -> uuid.UUID:
    """The vector-store id for a passage. A pure function of the passage's identity."""
    return uuid.uuid5(POINT_NAMESPACE, chunk_ref)


@dataclass(frozen=True, slots=True)
class VectorRecord:
    """One passage on its way into the index."""

    chunk_ref: str
    case_id: uuid.UUID
    document_id: uuid.UUID
    vector: list[float]
    content_hash: str
    page_number: int | None
    section_label: str | None


@dataclass(frozen=True, slots=True)
class VectorHit:
    """One search result. Text is hydrated from Postgres by the caller."""

    chunk_ref: str
    score: float


@runtime_checkable
class VectorStore(Protocol):
    """What the pipeline needs from a vector index."""

    name: str

    async def ensure_ready(self, *, dimension: int) -> None:
        """Create the collection if it is absent. Idempotent."""

    async def upsert(self, records: Sequence[VectorRecord]) -> int:
        """Write points, overwriting any with the same id. Returns the count."""

    async def delete_for_document(self, document_id: uuid.UUID) -> None:
        """Remove every point belonging to one document."""

    async def delete_for_case(self, case_id: uuid.UUID) -> None:
        """Remove every point belonging to one case.

        One filtered delete rather than a loop over the case's documents: a case
        that lost a document row before the vectors were cleared would otherwise
        keep those points forever, because nothing left in Postgres can name them.
        """

    async def search(
        self,
        vector: list[float],
        *,
        case_id: uuid.UUID,
        limit: int,
        document_id: uuid.UUID | None = None,
    ) -> list[VectorHit]:
        """Nearest passages within one case."""

    async def aclose(self) -> None: ...


class QdrantVectorStore:
    """Qdrant over HTTP.

    One collection for every case, scoped by a payload filter on `fnol_case_id` —
    which is what the payload index below exists for. IIF creates no payload indexes
    at all, so every one of its filters is an unindexed scan; at a few thousand points
    that is invisible and at a few million it is the whole latency budget.
    """

    name = "qdrant"

    def __init__(self, config: DocumentIntelligenceSettings | None = None) -> None:
        self._config = config or settings.docint
        if not self._config.qdrant_url:
            raise ValueError("QdrantVectorStore requires a URL.")
        self._client: Any | None = None
        self._ready = False

    def _connect(self) -> Any:
        if self._client is None:
            from qdrant_client import AsyncQdrantClient

            self._client = AsyncQdrantClient(
                url=self._config.qdrant_url,
                api_key=self._config.qdrant_api_key or None,
                timeout=int(self._config.qdrant_timeout_seconds),
                # HTTP rather than gRPC so nothing in the image needs a native build.
                prefer_grpc=False,
            )
        return self._client

    async def ensure_ready(self, *, dimension: int) -> None:
        if self._ready:
            return

        from qdrant_client.models import Distance, PayloadSchemaType, VectorParams

        client = self._connect()
        collection = self._config.qdrant_collection

        existing = await client.get_collections()
        names = {entry.name for entry in existing.collections}
        if collection not in names:
            await client.create_collection(
                collection_name=collection,
                vectors_config=VectorParams(size=dimension, distance=Distance.COSINE),
            )
            logger.info("qdrant_collection_created", collection=collection, dimension=dimension)

        # Idempotent, and cheap when they already exist. Every filter this store
        # issues is on one of these three.
        for field, schema in (
            ("fnol_case_id", PayloadSchemaType.KEYWORD),
            ("fnol_document_id", PayloadSchemaType.KEYWORD),
            ("content_hash", PayloadSchemaType.KEYWORD),
        ):
            # Already-indexed is the normal case on every call after the first, and
            # Qdrant reports it as an error rather than as a no-op.
            with contextlib.suppress(Exception):
                await client.create_payload_index(
                    collection_name=collection, field_name=field, field_schema=schema
                )

        self._ready = True

    async def upsert(self, records: Sequence[VectorRecord]) -> int:
        if not records:
            return 0

        from qdrant_client.models import PointStruct

        client = self._connect()
        points = [
            PointStruct(
                id=str(point_id_for(record.chunk_ref)),
                vector=record.vector,
                payload={
                    "chunk_ref": record.chunk_ref,
                    "fnol_case_id": str(record.case_id),
                    "fnol_document_id": str(record.document_id),
                    "content_hash": record.content_hash,
                    "page_number": record.page_number,
                    "section_label": record.section_label,
                },
            )
            for record in records
        ]

        for start in range(0, len(points), UPSERT_BATCH_SIZE):
            await client.upsert(
                collection_name=self._config.qdrant_collection,
                points=points[start : start + UPSERT_BATCH_SIZE],
                wait=True,
            )
        return len(points)

    async def delete_for_document(self, document_id: uuid.UUID) -> None:
        await self._delete_where("fnol_document_id", document_id)

    async def delete_for_case(self, case_id: uuid.UUID) -> None:
        await self._delete_where("fnol_case_id", case_id)

    async def _delete_where(self, key: str, value: uuid.UUID) -> None:
        """Delete by payload filter. Both indexed keys, so this is not a scan."""
        from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue

        client = self._connect()
        await client.delete(
            collection_name=self._config.qdrant_collection,
            points_selector=FilterSelector(
                filter=Filter(must=[FieldCondition(key=key, match=MatchValue(value=str(value)))])
            ),
            wait=True,
        )

    async def search(
        self,
        vector: list[float],
        *,
        case_id: uuid.UUID,
        limit: int,
        document_id: uuid.UUID | None = None,
    ) -> list[VectorHit]:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        conditions = [FieldCondition(key="fnol_case_id", match=MatchValue(value=str(case_id)))]
        if document_id is not None:
            conditions.append(
                FieldCondition(key="fnol_document_id", match=MatchValue(value=str(document_id)))
            )

        client = self._connect()
        response = await client.query_points(
            collection_name=self._config.qdrant_collection,
            query=vector,
            query_filter=Filter(must=conditions),
            limit=limit,
            with_payload=True,
        )

        hits: list[VectorHit] = []
        for point in response.points:
            payload = point.payload or {}
            ref = payload.get("chunk_ref")
            if isinstance(ref, str):
                hits.append(VectorHit(chunk_ref=ref, score=float(point.score)))
        return hits

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
            self._ready = False


# --- factory ------------------------------------------------------------------

_store: VectorStore | None = None
_built = False


def get_vector_store() -> VectorStore | None:
    """The process-wide store, or `None` when no vector store is configured.

    `None` means keyword-only retrieval, which is a real fallback rather than a
    degraded stub — the passages are in Postgres with a full-text index over them.
    """
    global _store, _built
    if not _built:
        if not settings.docint.vector_configured:
            logger.info("vector_store_not_configured")
            _store = None
        else:
            _store = QdrantVectorStore()
            logger.info(
                "vector_store_ready",
                url=settings.docint.qdrant_url,
                collection=settings.docint.qdrant_collection,
            )
        _built = True
    return _store


def set_vector_store(store: VectorStore | None) -> None:
    """Override the store. The seam tests use."""
    global _store, _built
    _store = store
    _built = True


def reset_vector_store() -> None:
    global _store, _built
    _store = None
    _built = False


async def close_vector_store() -> None:
    global _store, _built
    current, _store, _built = _store, None, False
    if current is not None:
        await current.aclose()


__all__ = [
    "POINT_NAMESPACE",
    "UPSERT_BATCH_SIZE",
    "QdrantVectorStore",
    "VectorHit",
    "VectorRecord",
    "VectorStore",
    "close_vector_store",
    "get_vector_store",
    "point_id_for",
    "reset_vector_store",
    "set_vector_store",
]
