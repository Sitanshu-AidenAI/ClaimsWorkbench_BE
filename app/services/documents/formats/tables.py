"""Rendering a table as text a reader can use.

A table is the one structure where dropping the layout destroys the meaning: a
cell reading `42,000` is a number, and `Estimated repair cost | 42,000` is a fact.
Both the model and the deterministic reader downstream see only text, so the
layout has to survive as text.

Which text depends on the table's shape, and the two shapes want opposite things:

* A **matrix** — a schedule of values, a loss run, a line-item estimate — is a
  grid, and the column heading is what gives every cell below it meaning. Rendered
  as a GFM pipe table, header row included.
* A **form** — the two-column `Policy number | POL-2026-0041` layout every claim
  notification form in the world uses — has no meaningful column headings, and
  rendering it as a grid buries each label one cell away from its value. Rendered
  as `Label: value` lines.

The second case earns its keep twice. `app/domain/heuristics.py` matches
`Label: value` anchored to the start of a line, so a claim form's table becomes
readable by the deterministic extractor as well as by the model — and a pipe table
would not be, because `|` is not one of the line prefixes that reader allows.
"""

from __future__ import annotations

from collections.abc import Sequence

#: Below this many columns a table cannot be a matrix worth a grid.
_MIN_MATRIX_COLUMNS = 3

#: At or above this share of blank header cells the first row is not a header,
#: whatever its position. Half of a header row being empty is enough: a claims
#: spreadsheet routinely puts one label/value pair beside three spacer columns, and
#: drawing a grid around that invents column headings the document never had.
_MAX_BLANK_HEADER_RATIO = 0.5

#: A form's first column is its labels, so nearly every row must have one. Below
#: this share the two columns are data rather than label/value.
_MIN_LABELLED_ROW_RATIO = 0.6


def render_table(rows: Sequence[Sequence[object]]) -> str:
    """Render one table. Returns `""` when there is nothing worth rendering."""
    grid = _normalise(rows)
    if not grid:
        return ""

    if _is_form(grid):
        return _render_form(grid)
    return _render_matrix(grid)


def _normalise(rows: Sequence[Sequence[object]]) -> list[list[str]]:
    """Strip every cell, then drop rows and columns that hold nothing.

    Empty columns are dropped as well as empty rows because a spreadsheet's used
    range routinely extends past its content — a table exported with four spacer
    columns would otherwise render as `| A |  |  |  | B |`, which costs prompt
    characters to say nothing.
    """
    grid = [[_cell(value) for value in row] for row in rows]
    grid = [row for row in grid if any(row)]
    if not grid:
        return []

    width = max(len(row) for row in grid)
    grid = [row + [""] * (width - len(row)) for row in grid]

    keep = [index for index in range(width) if any(row[index] for row in grid)]
    if not keep:
        return []
    return [[row[index] for index in keep] for row in grid]


def _cell(value: object) -> str:
    if value is None:
        return ""
    # A pipe inside a cell would close the column early in the rendered table.
    return str(value).replace("|", "/").replace("\n", " ").strip()


def _is_form(grid: list[list[str]]) -> bool:
    header, *body = grid
    if not body:
        # A single row is a row of labels or a row of values; either way there is
        # no grid to draw, and `Label: value` cannot mislead.
        return True

    blank_headers = sum(1 for cell in header if not cell) / len(header)
    if blank_headers >= _MAX_BLANK_HEADER_RATIO:
        return True

    if len(header) >= _MIN_MATRIX_COLUMNS:
        return False

    labelled = sum(1 for row in body if row[0]) / len(body)
    return labelled >= _MIN_LABELLED_ROW_RATIO


def _render_form(grid: list[list[str]]) -> str:
    """`Label: value` per row, with extra columns appended after the first value.

    The header row is *not* skipped: in a form its first row is a label/value pair
    like any other, and dropping it would lose a field.
    """
    lines: list[str] = []
    for row in grid:
        label, *values = row
        joined = " ".join(value for value in values if value).strip()
        if label and joined:
            lines.append(f"{label}: {joined}")
        elif joined:
            lines.append(joined)
        elif label:
            lines.append(label)
    return "\n".join(lines)


def _render_matrix(grid: list[list[str]]) -> str:
    """A GFM pipe table.

    Cells are not padded to a common width. Padding is what makes a pipe table
    readable in a terminal, and this table's only readers are a language model and
    a regex — neither of which cares, while the spaces are charged against the
    prompt budget.
    """
    header, *body = grid
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


__all__ = ["render_table"]
