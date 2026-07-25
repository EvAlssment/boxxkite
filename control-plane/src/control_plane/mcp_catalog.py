"""Curated outbound-MCP catalog resolution (GitHub issue #117,
docs/OUTBOUND-MCP-DESIGN.md §4).

Mirrors `image_builder.py`'s `render_dockerfile`/`UnknownBaseError` pattern
exactly, applied to catalog hostnames instead of base-image digests:
`schemas.py`'s `McpCatalogId` Literal enumerates the caller-facing enum of
legal `catalog_id` values, and `settings.BOXKITE_MCP_CATALOG` is the
separate, operator-configurable env-var-driven mapping from those same
names to a real hostname. The two must never drift apart -- a Literal value
with no matching catalog entry is a configuration bug, not a client error,
and this module's job is to fail loudly (not silently resolve to nothing)
if that ever happens.
"""

from __future__ import annotations

from .config import settings


class UnknownMcpCatalogEntryError(ValueError):
    """Raised when a `catalog_id` has no entry in
    `settings.BOXKITE_MCP_CATALOG` -- guards against schemas.py's
    `McpCatalogId` Literal and this config dict drifting out of sync (e.g.
    a new catalog_id added to one but not the other), the same drift this
    module's docstring calls out and image_builder.py's `UnknownBaseError`
    already guards against for `base`/`BOXKITE_BASE_IMAGE_REFS`."""


def resolve_catalog_host(catalog_id: str) -> str:
    """Resolve a `catalog_id` to its curated, fixed API hostname.

    Raises `UnknownMcpCatalogEntryError` rather than returning `None` --
    every caller of this function is on the write path (creating an
    `McpConnection` row), where a config drift is a "must not silently
    proceed" condition, not a "return no rows" one the way a lookup miss
    on a read path would be.
    """
    host = settings.BOXKITE_MCP_CATALOG.get(catalog_id)
    if not host:
        raise UnknownMcpCatalogEntryError(
            f"No catalog entry configured for catalog_id {catalog_id!r} -- check "
            "BOXKITE_MCP_CATALOG_RAW/schemas.py's McpCatalogId Literal are in sync"
        )
    return host
