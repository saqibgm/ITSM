"""Shared Pydantic response shapes."""

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None
    total: int | None = None


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str
    details: dict | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail
