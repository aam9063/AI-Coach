"""The OpenAPI schema must be unambiguous.

Duplicate operation ids are a real defect in the published contract, not a
cosmetic warning: an OpenAPI consumer keys operations by id, so two operations
sharing one id can only ever yield one method in a generated client (or collide
on the second). FastAPI derives the id from the function name plus path, so a
SINGLE function registered for several HTTP methods produces two operations that
share it -- which is exactly what the chart media route did while it was declared
as ``api_route(..., methods=["GET", "HEAD"])``.

The suite ran for weeks emitting the warning and nobody read it; a schema-level
guard turns it into a failure.
"""

from __future__ import annotations

from collections import Counter

from app.main import create_app

_HTTP_METHODS = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}


def test_operation_ids_are_unique_across_the_schema() -> None:
    app = create_app()
    schema = app.openapi()

    ids = [
        operation["operationId"]
        for path_item in schema.get("paths", {}).values()
        for method, operation in path_item.items()
        if method in _HTTP_METHODS and "operationId" in operation
    ]

    assert ids, "the schema exposes no operations at all"

    duplicates = sorted(
        name for name, count in Counter(ids).items() if count > 1
    )
    assert duplicates == [], (
        "duplicate operationIds in the OpenAPI schema: "
        f"{duplicates} -- give each operation its own id or keep one method "
        "out of the schema"
    )
