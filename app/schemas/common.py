"""Shared Pydantic schema building blocks."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class SchemaBase(BaseModel):
    """Base for request/response models.

    `from_attributes` lets responses be built straight from ORM instances or
    asyncpg records; `populate_by_name` allows camelCase aliases where a screen
    needs them.
    """

    model_config = ConfigDict(
        from_attributes=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class PageParams(BaseModel):
    """Cursor-free pagination parameters, suitable as a query-param dependency."""

    page: Annotated[int, Field(ge=1)] = 1
    page_size: Annotated[int, Field(ge=1, le=200)] = 25

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


class Page[T](SchemaBase):
    """A page of results plus the totals a table needs to render."""

    items: list[T]
    total: int
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        if self.page_size == 0:
            return 0
        return -(-self.total // self.page_size)


class ErrorDetail(SchemaBase):
    code: str
    message: str
    details: dict[str, object] = Field(default_factory=dict)


class ErrorResponse(SchemaBase):
    """The single error shape every failing endpoint returns."""

    error: ErrorDetail
    request_id: str | None = None
