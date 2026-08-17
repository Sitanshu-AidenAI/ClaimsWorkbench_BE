"""Loading a dataset, and seeding the ones that ship with the product.

Two jobs, and they are here together because they are two views of one thing:
the bundled JSON under `app/data/extraction_schemas/` is the *source* of the
built-in datasets, and the database rows are the *editable copy*. Seeding is
therefore additive and never destructive — it creates what is missing and leaves
alone what an administrator has changed, because the whole point of the module is
that the field list belongs to the desk rather than to the release.

The one exception is a field that is genuinely new in a release: it is added, so
upgrading actually delivers the new question. A field an administrator deleted
stays deleted, which is why the seed records nothing about deletion — a row that
is absent because it was never created and a row that is absent because it was
removed are indistinguishable, and the safe reading of the two is "add it".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.domain.enums import ExtractionSchemaStatus
from app.models.extraction import ExtractionSchema, ExtractionSchemaField
from app.repositories.extraction import ExtractionSchemaRepository
from app.services.extraction.schema import DatasetSchema, FieldSpec, normalise_data_type

logger = get_logger(__name__)

#: Where the bundled datasets live. One JSON file per dataset, named after its key.
SEED_DIRECTORY = Path(__file__).resolve().parents[2] / "data" / "extraction_schemas"

#: The dataset the FNOL pipeline falls back to when nothing is configured and
#: nothing is flagged as the default.
BUILTIN_FNOL_KEY = "fnol_notice"


def load_seed_files(directory: Path | None = None) -> list[dict[str, Any]]:
    """Read the bundled dataset definitions off disk, in a stable order."""
    root = directory or SEED_DIRECTORY
    if not root.is_dir():
        return []
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(root.glob("*.json"))]


async def seed_builtin_schemas(
    repository: ExtractionSchemaRepository, *, directory: Path | None = None
) -> dict[str, int]:
    """Create the bundled datasets and add any fields a release introduced.

    Idempotent and safe to run on every boot. Returns what it did, so a startup
    log line can say "0 schemas, 0 fields" on the common path rather than being
    silent about work it did not do.
    """
    created_schemas = 0
    created_fields = 0

    for definition in load_seed_files(directory):
        key = str(definition.get("key") or "").strip()
        if not key:
            logger.warning("extraction_seed_missing_key", file=str(definition)[:120])
            continue

        existing = await repository.get_by_key(key)
        if existing is None:
            repository.add(_schema_from_definition(definition))
            await repository.flush()
            created_schemas += 1
            created_fields += len(definition.get("fields") or [])
            continue

        added = _add_missing_fields(existing, definition)
        if added:
            existing.version += 1
            created_fields += added
            await repository.flush()

    if created_schemas or created_fields:
        logger.info("extraction_schemas_seeded", schemas=created_schemas, fields=created_fields)
    return {"schemas": created_schemas, "fields": created_fields}


def _schema_from_definition(definition: dict[str, Any]) -> ExtractionSchema:
    schema = ExtractionSchema(
        key=str(definition["key"]),
        name=str(definition.get("name") or definition["key"]),
        description=definition.get("description"),
        version=1,
        status=ExtractionSchemaStatus.ACTIVE,
        is_builtin=bool(definition.get("is_builtin", True)),
        is_default=bool(definition.get("is_default", False)),
        review_threshold=float(definition.get("review_threshold", 0.6)),
        created_by="system",
    )
    schema.fields = [
        _field_from_definition(raw, position)
        for position, raw in enumerate(definition.get("fields") or [])
    ]
    return schema


def _field_from_definition(raw: dict[str, Any], position: int) -> ExtractionSchemaField:
    return ExtractionSchemaField(
        key=str(raw["key"]),
        label=str(raw.get("label") or raw["key"]),
        description=str(raw.get("description") or raw.get("label") or raw["key"]),
        data_type=normalise_data_type(raw.get("data_type")),
        group_label=str(raw.get("group_label") or "Fields"),
        required=bool(raw.get("required", False)),
        enabled=bool(raw.get("enabled", True)),
        position=int(raw.get("position", position)),
        aliases=list(raw.get("aliases") or []) or None,
        extraction_hint=raw.get("extraction_hint"),
    )


def _add_missing_fields(schema: ExtractionSchema, definition: dict[str, Any]) -> int:
    """Append fields the release added and this deployment does not have yet."""
    have = {row.key for row in schema.fields}
    highest = max((row.position for row in schema.fields), default=-1)
    added = 0
    for raw in definition.get("fields") or []:
        key = str(raw.get("key") or "")
        if not key or key in have:
            continue
        highest += 1
        schema.fields.append(_field_from_definition(raw, highest))
        added += 1
    return added


def to_dataset(schema: ExtractionSchema) -> DatasetSchema:
    """The in-memory view the engine works with.

    Disabled fields are dropped here rather than filtered by every caller: a
    field switched off is a question the desk has decided not to ask, and the
    engine should not have to remember to check.
    """
    specs = tuple(
        FieldSpec(
            key=row.key,
            label=row.label,
            description=row.description,
            data_type=normalise_data_type(row.data_type),
            group_label=row.group_label,
            required=row.required,
            position=row.position,
            aliases=tuple(row.aliases or ()),
            extraction_hint=row.extraction_hint,
        )
        for row in sorted(schema.fields, key=lambda row: (row.position, row.key))
        if row.enabled
    )
    return DatasetSchema(
        key=schema.key,
        name=schema.name,
        description=schema.description,
        version=schema.version,
        review_threshold=float(schema.review_threshold),
        fields=specs,
    )


__all__ = [
    "BUILTIN_FNOL_KEY",
    "SEED_DIRECTORY",
    "load_seed_files",
    "seed_builtin_schemas",
    "to_dataset",
]
