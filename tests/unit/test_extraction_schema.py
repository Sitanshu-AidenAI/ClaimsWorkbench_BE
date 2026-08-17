"""The dataset itself: coercion, the retrieval query, and seeding.

Nothing here touches a database or a model provider. The properties under test
are the ones every other part of the feature assumes without re-checking — that a
value which will not coerce is kept rather than dropped, that a field's query is
built from all three of its name, its description and its aliases, and that
seeding a release twice does not duplicate a dataset or undo an edit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.extraction.registry import (
    BUILTIN_FNOL_KEY,
    SEED_DIRECTORY,
    _add_missing_fields,
    _schema_from_definition,
    load_seed_files,
    to_dataset,
)
from app.services.extraction.schema import (
    DatasetSchema,
    FieldSpec,
    coerce_value,
    normalise_data_type,
    render_value,
    stores_typed_value,
)


class TestCoercion:
    """Every type, and the rule that a bad value is never discarded."""

    @pytest.mark.parametrize(
        ("text", "data_type", "expected"),
        [
            ("  CP-2026-4471 ", "string", "CP-2026-4471"),
            ("12", "integer", 12),
            ("1,240", "integer", 1240),
            ("none reported", "integer", 0),
            ("128,000.50", "number", 128000.50),
            ("GBP 128,000", "money", 12_800_000),
            ("08 March 2026", "date", "2026-03-08"),
            ("yes", "boolean", True),
            ("no", "boolean", False),
            ('[{"name": "A Broker"}]', "json", [{"name": "A Broker"}]),
        ],
    )
    def test_a_stated_value_becomes_its_declared_type(
        self, text: str, data_type: str, expected: object
    ) -> None:
        coerced, error = coerce_value(text, data_type)
        assert error is None
        assert coerced == expected

    def test_a_value_that_will_not_coerce_reports_why_rather_than_vanishing(self) -> None:
        """The most important property in the module.

        A model asked for a date returns something unparseable often enough that
        a coercion which raised, or returned `None` silently, would lose the
        value *and* its citation. A reviewer who can see "the thirteenth month"
        can correct it; a reviewer shown an empty box cannot.
        """
        coerced, error = coerce_value("sometime in the spring", "date")
        assert coerced is None
        assert error is not None
        assert "does not read as a date" in error

    def test_a_fenced_json_answer_is_still_json(self) -> None:
        coerced, error = coerce_value('```json\n{"role": "witness"}\n```', "json")
        assert error is None
        assert coerced == {"role": "witness"}

    def test_malformed_json_is_reported_not_raised(self) -> None:
        coerced, error = coerce_value("{not json", "json")
        assert coerced is None
        assert error == "The value is not valid JSON."

    def test_an_empty_value_is_absent_rather_than_invalid(self) -> None:
        assert coerce_value("   ", "integer") == (None, None)
        assert coerce_value(None, "money") == (None, None)

    @pytest.mark.parametrize(
        ("declared", "expected"),
        [("Number", "number"), ("Yes/No", "boolean"), ("array", "json"), ("wat", "string")],
    )
    def test_a_declared_type_is_mapped_onto_a_known_one(self, declared: str, expected: str) -> None:
        assert normalise_data_type(declared) == expected

    def test_only_a_type_with_a_distinct_coerced_form_stores_one(self) -> None:
        """A string's coerced form is the string, and storing it twice is noise."""
        assert not stores_typed_value("string")
        assert not stores_typed_value("text")
        assert stores_typed_value("money")
        assert stores_typed_value("date")

    def test_money_renders_back_as_major_units(self) -> None:
        assert render_value(12_800_000, "money") == "128000.00"

    def test_a_boolean_renders_as_a_word_not_a_python_repr(self) -> None:
        assert render_value(True, "boolean") == "true"


class TestFieldSpec:
    def test_the_query_carries_the_label_the_description_and_the_aliases(self) -> None:
        """All three, because a document may use any of them.

        The label is how the schema names the thing, the description is how a
        document describes it, and an alias is what a different house calls it.
        One embedding of all three is one search; three separate searches is
        three round trips for the same recall.
        """
        spec = FieldSpec(
            key="policy.policy_number",
            label="Policy number",
            description="The policy or certificate number the risk is written under.",
            aliases=("UMR", "cover note"),
        )
        query = spec.query
        assert "Policy number" in query
        assert "certificate number" in query
        assert "UMR" in query
        assert "cover note" in query

    def test_a_field_coerces_through_its_declared_type(self) -> None:
        spec = FieldSpec(
            key="loss.injuries", label="Injuries", description="…", data_type="integer"
        )
        assert spec.coerce("3 people") == (3, None)

    def test_no_injuries_is_zero_rather_than_missing(self) -> None:
        """ "None reported" is a fact somebody established, not an unanswered question.

        An empty injuries field means nobody looked. Zero means somebody did, and
        the completeness engine and the severity assessment read the difference.
        """
        spec = FieldSpec(
            key="loss.injuries", label="Injuries", description="…", data_type="integer"
        )
        assert spec.coerce("None reported.") == (0, None)
        assert spec.coerce("nil") == (0, None)


class TestDatasetSchema:
    def test_duplicate_field_keys_are_refused_at_construction(self) -> None:
        """Two fields with one key would silently overwrite each other's value."""
        with pytest.raises(ValueError, match="Duplicate field key"):
            DatasetSchema(
                key="x",
                name="X",
                fields=(
                    FieldSpec(key="a", label="A", description="a"),
                    FieldSpec(key="a", label="A again", description="a"),
                ),
            )

    def test_groups_are_listed_in_the_order_the_fields_declare_them(self) -> None:
        dataset = DatasetSchema(
            key="x",
            name="X",
            fields=(
                FieldSpec(key="a", label="A", description="a", group_label="Loss"),
                FieldSpec(key="b", label="B", description="b", group_label="Policy"),
                FieldSpec(key="c", label="C", description="c", group_label="Loss"),
            ),
        )
        assert dataset.groups == ("Loss", "Policy")


class TestBundledDatasets:
    def test_the_fnol_dataset_ships_and_is_the_default(self) -> None:
        definitions = {definition["key"]: definition for definition in load_seed_files()}
        fnol = definitions[BUILTIN_FNOL_KEY]
        assert fnol["is_default"] is True
        assert fnol["is_builtin"] is True
        assert len(fnol["fields"]) >= 25

    def test_every_bundled_field_has_a_description_worth_searching_with(self) -> None:
        """The description *is* the retrieval query.

        A one-word description is a field that will be found by keyword and by
        nothing else, which defeats the point of the layer. The floor is
        deliberately low and the check is deliberately blunt: it catches a field
        added in a hurry, which is the only failure mode that actually occurs.
        """
        for definition in load_seed_files():
            for field in definition["fields"]:
                description = field["description"]
                assert len(description) > 60, f"{field['key']} has a thin description"
                assert description.strip().endswith("."), f"{field['key']} reads as a fragment"

    def test_every_bundled_field_declares_a_known_type(self) -> None:
        for definition in load_seed_files():
            for field in definition["fields"]:
                declared = field.get("data_type", "string")
                assert normalise_data_type(declared) == declared, (
                    f"{field['key']} declares {declared!r}, which is not a known type"
                )

    def test_the_bundled_keys_match_what_the_claim_record_writes_back(self) -> None:
        """The built-in dataset's keys are the review screen's field paths.

        That correspondence is what lets the adapter mirror a value onto the
        claim record with no translation table. A key changed on one side and not
        the other would silently stop writing back, and the value would appear on
        the dataset panel and nowhere else.
        """
        from app.services.fnol.adapter import FNOL_WRITEBACK

        definitions = {definition["key"]: definition for definition in load_seed_files()}
        keys = {field["key"] for field in definitions[BUILTIN_FNOL_KEY]["fields"]}
        assert keys == set(FNOL_WRITEBACK)


class TestSeeding:
    def test_a_definition_becomes_a_schema_with_its_fields_in_order(self) -> None:
        schema = _schema_from_definition(
            {
                "key": "slip",
                "name": "Slip",
                "fields": [
                    {"key": "b", "label": "B", "description": "b", "position": 1},
                    {"key": "a", "label": "A", "description": "a", "position": 0},
                ],
            }
        )
        assert schema.key == "slip"
        assert [field.key for field in schema.fields] == ["b", "a"]
        assert [field.position for field in schema.fields] == [1, 0]

    def test_seeding_an_existing_dataset_adds_only_what_is_new(self, tmp_path: Path) -> None:
        """Additive, never destructive.

        A release that introduces a field should deliver it. An administrator who
        rewrote a description must keep their wording. Both are the same code
        path, and this is the test that says which one wins where they meet.
        """
        schema = _schema_from_definition(
            {
                "key": "x",
                "name": "X",
                "fields": [{"key": "a", "label": "A", "description": "the original wording"}],
            }
        )
        schema.fields[0].description = "an administrator's wording"

        added = _add_missing_fields(
            schema,
            {
                "key": "x",
                "fields": [
                    {"key": "a", "label": "A", "description": "the release's new wording"},
                    {"key": "b", "label": "B", "description": "a field this release adds"},
                ],
            },
        )

        assert added == 1
        assert schema.fields[0].description == "an administrator's wording"
        assert [field.key for field in schema.fields] == ["a", "b"]
        assert schema.fields[1].position == 1

    def test_a_field_the_administrator_deleted_comes_back(self, tmp_path: Path) -> None:
        """Documented rather than defended.

        The seed cannot tell a field that was never created from one that was
        removed, so it adds both. Turning a field *off* is the supported way to
        stop asking a question, and `enabled=false` survives seeding because the
        key is still present.
        """
        schema = _schema_from_definition(
            {"key": "x", "name": "X", "fields": [{"key": "a", "label": "A", "description": "a"}]}
        )
        schema.fields.clear()
        added = _add_missing_fields(
            schema, {"key": "x", "fields": [{"key": "a", "label": "A", "description": "a"}]}
        )
        assert added == 1

    def test_load_reads_every_bundled_file(self, tmp_path: Path) -> None:
        (tmp_path / "one.json").write_text(json.dumps({"key": "one", "fields": []}))
        (tmp_path / "two.json").write_text(json.dumps({"key": "two", "fields": []}))
        assert [definition["key"] for definition in load_seed_files(tmp_path)] == ["one", "two"]

    def test_an_absent_seed_directory_is_not_an_error(self, tmp_path: Path) -> None:
        assert load_seed_files(tmp_path / "nowhere") == []

    def test_the_seed_directory_is_where_the_bundled_datasets_actually_are(self) -> None:
        assert (SEED_DIRECTORY / "fnol_notice.json").is_file()


class TestToDataset:
    def test_a_disabled_field_is_not_offered_to_the_engine(self) -> None:
        """Switching a field off must stop it being asked, everywhere.

        Filtered here rather than by each caller: the engine, the prompt and the
        fingerprint all read the dataset, and a filter any one of them forgot
        would leave a question being asked that the desk decided to stop asking.
        """
        schema = _schema_from_definition(
            {
                "key": "x",
                "name": "X",
                "fields": [
                    {"key": "a", "label": "A", "description": "a"},
                    {"key": "b", "label": "B", "description": "b", "enabled": False},
                ],
            }
        )
        dataset = to_dataset(schema)
        assert [spec.key for spec in dataset.fields] == ["a"]
