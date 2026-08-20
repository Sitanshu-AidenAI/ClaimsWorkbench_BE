"""The policy collection.

Behind a Protocol, exactly as the claim-side store is, and for the same two
reasons: Qdrant is the most replaceable part of this pipeline, and a test needs an
in-memory one that behaves the same way — including on the parts that are easy to
get wrong.

**Its own collection, never a shared one with a discriminator field.** Mixing
policy wordings and claim passages into one collection and filtering on a `kind`
payload key would work and is the wrong trade. A carrier's policy book and its
claim documents have different retention periods, different access-control
stories, and different lifecycles — a policy stays indexed for the length of its
term, a claim's passages are destroyed when the claim is. A filter is a promise the
application makes; a collection is a promise the datastore makes, and this is a
boundary where the second is what is wanted. It also means "rebuild the policy
index" is one `delete_collection` rather than a filtered delete that has to be
exactly right.

**Point ids are deterministic** — `uuid5` of the passage's ref, shared with the
claim side through `point_id_for`. Re-ingesting a document overwrites its points in
place rather than adding a second copy of every clause that nothing can ever name
again. Neither the content hash nor the embedding model is in the id, for the
reasons `app/services/intelligence/vectors.py` sets out.

**The payload carries filter keys and no text.** Postgres owns the excerpt. What is
here is what a *filtered* search needs — the policy, the document, the line of
business, the term — because the accuracy win in this module is filtering the
library down before ranking it, and a filter on an unindexed payload key is a scan.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol, runtime_checkable

from app.core.config import DocumentIntelligenceSettings, PolicyLibrarySettings, settings
from app.core.logging import get_logger
from app.services.intelligence.vectors import point_id_for

logger = get_logger(__name__)

#: Points per upsert call. A thirty-page policy is a few hundred passages, so this
#: is two or three round trips per document rather than one per passage.
UPSERT_BATCH_SIZE = 100


@dataclass(frozen=True, slots=True)
class PolicyVectorRecord:
    """One policy passage on its way into the index."""

    chunk_ref: str
    document_id: uuid.UUID
    vector: list[float]
    content_hash: str
    policy_id: uuid.UUID | None = None
    policy_number: str | None = None
    line_of_business: str | None = None
    page_number: int | None = None
    section_label: str | None = None
    effective_date: date | None = None
    expiry_date: date | None = None


@dataclass(frozen=True, slots=True)
class PolicyVectorHit:
    """One search result. Text and metadata are hydrated from Postgres."""

    chunk_ref: str
    score: float
    document_id: uuid.UUID | None = None


@runtime_checkable
class PolicyVectorStore(Protocol):
    """What policy matching needs from a vector index."""

    name: str

    async def ensure_ready(self, *, dimension: int) -> None:
        """Create the collection if it is absent. Idempotent."""

    async def upsert(self, records: Sequence[PolicyVectorRecord]) -> int:
        """Write points, overwriting any with the same id. Returns the count."""

    async def delete_for_document(self, document_id: uuid.UUID) -> None:
        """Remove every point belonging to one policy document."""

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
        """The nearest policy passages, optionally narrowed.

        `line_of_business` is the filter that matters for accuracy: a liability
        notice retrieved against property wordings competes with clauses that share
        its vocabulary and answer none of its questions. It is a *filter* rather
        than a scoring signal because a policy of the wrong line is not a weaker
        answer — it is not an answer.
        """

    async def aclose(self) -> None: ...


class QdrantPolicyVectorStore:
    """Qdrant over HTTP, on the policy collection."""

    name = "qdrant-policies"

    def __init__(
        self,
        config: PolicyLibrarySettings | None = None,
        transport: DocumentIntelligenceSettings | None = None,
    ) -> None:
        self._config = config or settings.policy_library
        #: Connection details are the deployment's, and there is one Qdrant. The
        #: policy block owns only the collection name.
        self._transport = transport or settings.docint
        if not self._transport.qdrant_url:
            raise ValueError("QdrantPolicyVectorStore requires a Qdrant URL.")
        self._client: Any | None = None
        self._ready = False

    def _connect(self) -> Any:
        if self._client is None:
            from qdrant_client import AsyncQdrantClient

            self._client = AsyncQdrantClient(
                url=self._transport.qdrant_url,
                api_key=self._transport.qdrant_api_key or None,
                timeout=int(self._transport.qdrant_timeout_seconds),
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
        if collection not in {entry.name for entry in existing.collections}:
            await client.create_collection(
                collection_name=collection,
                vectors_config=VectorParams(size=dimension, distance=Distance.COSINE),
            )
            logger.info("policy_collection_created", collection=collection, dimension=dimension)

        # Every filter this store issues is on one of these. Idempotent, and cheap
        # when they already exist — Qdrant reports already-indexed as an error
        # rather than as a no-op, which is what the suppression is for.
        for field, schema in (
            ("policy_document_id", PayloadSchemaType.KEYWORD),
            ("policy_id", PayloadSchemaType.KEYWORD),
            ("policy_number", PayloadSchemaType.KEYWORD),
            ("line_of_business", PayloadSchemaType.KEYWORD),
            ("content_hash", PayloadSchemaType.KEYWORD),
        ):
            with contextlib.suppress(Exception):
                await client.create_payload_index(
                    collection_name=collection, field_name=field, field_schema=schema
                )

        self._ready = True

    async def upsert(self, records: Sequence[PolicyVectorRecord]) -> int:
        if not records:
            return 0

        from qdrant_client.models import PointStruct

        client = self._connect()
        points = [
            PointStruct(
                id=str(point_id_for(record.chunk_ref)),
                vector=record.vector,
                payload=_payload(record),
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
        """Remove one document's points. A collection that does not exist is success.

        The distinction matters and getting it wrong breaks a real path. Deleting a
        wording from a library that was never vectorised — no embedding provider when it
        was ingested, or a collection lost with a volume — asks Qdrant to delete from a
        collection that is not there, and it answers 404. That is not a failure of this
        operation: there are no points, which is the state the caller wanted.

        A *connection* failure is still raised, and must be. `PolicyLibraryService.remove`
        deletes the vectors before the row precisely so that a failure here leaves the row
        in place and the operation retryable — swallowing everything would turn that into
        orphan points nothing in Postgres can ever name again.
        """
        from qdrant_client.models import FieldCondition, Filter, FilterSelector, MatchValue

        client = self._connect()
        try:
            await client.delete(
                collection_name=self._config.qdrant_collection,
                points_selector=FilterSelector(
                    filter=Filter(
                        must=[
                            FieldCondition(
                                key="policy_document_id",
                                match=MatchValue(value=str(document_id)),
                            )
                        ]
                    )
                ),
                wait=True,
            )
        except Exception as exc:
            if not _is_missing_collection(exc):
                raise
            logger.info(
                "policy_vector_collection_absent",
                collection=self._config.qdrant_collection,
                document_id=str(document_id),
            )

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
        from qdrant_client.models import (
            FieldCondition,
            Filter,
            MatchAny,
            MatchValue,
        )

        conditions: list[Any] = []
        if document_id is not None:
            conditions.append(
                FieldCondition(key="policy_document_id", match=MatchValue(value=str(document_id)))
            )
        if document_ids:
            conditions.append(
                FieldCondition(
                    key="policy_document_id",
                    match=MatchAny(any=[str(value) for value in document_ids]),
                )
            )
        if policy_id is not None:
            conditions.append(
                FieldCondition(key="policy_id", match=MatchValue(value=str(policy_id)))
            )
        if line_of_business:
            conditions.append(
                FieldCondition(key="line_of_business", match=MatchValue(value=line_of_business))
            )

        client = self._connect()
        response = await client.query_points(
            collection_name=self._config.qdrant_collection,
            query=vector,
            query_filter=Filter(must=conditions) if conditions else None,
            limit=limit,
            with_payload=True,
        )

        hits: list[PolicyVectorHit] = []
        for point in response.points:
            payload = point.payload or {}
            ref = payload.get("chunk_ref")
            if not isinstance(ref, str):
                continue
            raw_document = payload.get("policy_document_id")
            document = None
            if isinstance(raw_document, str):
                with contextlib.suppress(ValueError):
                    document = uuid.UUID(raw_document)
            hits.append(
                PolicyVectorHit(chunk_ref=ref, score=float(point.score), document_id=document)
            )
        return hits

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
            self._ready = False


def _is_missing_collection(exc: Exception) -> bool:
    """Whether a Qdrant error means "that collection is not there".

    Read off the response *status*, not the message. The client raises one exception type
    for every unexpected response, so the type alone cannot distinguish a 404 from a 503 —
    but `status_code` is a typed attribute and 404 on a collection-scoped call has exactly
    one meaning. Matching on the message text would be the substring-classification
    mistake this codebase refuses everywhere else.
    """
    return getattr(exc, "status_code", None) == 404


def _payload(record: PolicyVectorRecord) -> dict[str, Any]:
    """The point's payload. Filter keys and identifiers; never passage text."""
    return {
        "chunk_ref": record.chunk_ref,
        "policy_document_id": str(record.document_id),
        "policy_id": str(record.policy_id) if record.policy_id else None,
        "policy_number": record.policy_number,
        "line_of_business": record.line_of_business,
        "content_hash": record.content_hash,
        "page_number": record.page_number,
        "section_label": record.section_label,
        "effective_date": record.effective_date.isoformat() if record.effective_date else None,
        "expiry_date": record.expiry_date.isoformat() if record.expiry_date else None,
    }


# --- factory ------------------------------------------------------------------
#
# The same shape as every other optional collaborator in this codebase: built once
# per process, and `None` is a legitimate answer rather than a missing one. `None`
# here means policy matching runs on Postgres full text alone, which for a policy
# number and an insured name is a genuinely good backend rather than a stub.

_store: PolicyVectorStore | None = None
_built = False


def get_policy_vector_store() -> PolicyVectorStore | None:
    """The process-wide policy store, or `None` when no vector store is configured."""
    global _store, _built
    if not _built:
        if not (settings.policy_library.enabled and settings.docint.vector_configured):
            logger.info("policy_vector_store_not_configured")
            _store = None
        else:
            _store = QdrantPolicyVectorStore()
            logger.info(
                "policy_vector_store_ready",
                url=settings.docint.qdrant_url,
                collection=settings.policy_library.qdrant_collection,
            )
        _built = True
    return _store


def set_policy_vector_store(store: PolicyVectorStore | None) -> None:
    """Override the store. The seam tests use."""
    global _store, _built
    _store = store
    _built = True


def reset_policy_vector_store() -> None:
    global _store, _built
    _store = None
    _built = False


async def close_policy_vector_store() -> None:
    global _store, _built
    current, _store, _built = _store, None, False
    if current is not None:
        await current.aclose()


__all__ = [
    "UPSERT_BATCH_SIZE",
    "PolicyVectorHit",
    "PolicyVectorRecord",
    "PolicyVectorStore",
    "QdrantPolicyVectorStore",
    "close_policy_vector_store",
    "get_policy_vector_store",
    "point_id_for",
    "reset_policy_vector_store",
    "set_policy_vector_store",
]
