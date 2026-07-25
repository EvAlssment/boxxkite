"""Curated outbound-MCP catalog (GitHub issue #117, docs/OUTBOUND-MCP-DESIGN.md
§4) -- config parsing (mirrors BOXKITE_BASE_IMAGE_REFS_RAW) and the
catalog_id-vs-config drift guard (mirrors image_builder.py's UnknownBaseError
test in test_image_builder_dockerfile.py).
"""

from __future__ import annotations

import pytest

from control_plane.config import settings
from control_plane.mcp_catalog import UnknownMcpCatalogEntryError, resolve_catalog_host
from control_plane.schemas import McpCatalogId
from typing import get_args


def test_default_catalog_has_an_entry_for_every_shipped_provider():
    catalog = settings.BOXKITE_MCP_CATALOG
    assert catalog["slack"] == "mcp.slack.com"
    assert catalog["notion"] == "mcp.notion.com"
    assert catalog["linear"] == "mcp.linear.app"
    assert catalog["github"] == "api.githubcopilot.com"


def test_resolve_catalog_host_returns_configured_host():
    assert resolve_catalog_host("slack") == "mcp.slack.com"


def test_resolve_catalog_host_unknown_id_raises():
    with pytest.raises(UnknownMcpCatalogEntryError):
        resolve_catalog_host("not-a-real-provider")


@pytest.mark.parametrize("catalog_id", get_args(McpCatalogId))
def test_every_schema_literal_value_has_a_matching_catalog_entry(catalog_id: str):
    """The defensive drift check (mirrors image_builder.py:132-136's
    precedent, tested directly in test_image_builder_dockerfile.py's
    test_render_dockerfile_unknown_base_raises): every McpCatalogId Literal
    value must resolve against the default BOXKITE_MCP_CATALOG_RAW, or a
    connection request for that catalog_id would 500 instead of 201."""
    assert resolve_catalog_host(catalog_id)


def test_catalog_raw_parsing_handles_trailing_commas_and_whitespace():
    original = settings.BOXKITE_MCP_CATALOG_RAW
    try:
        settings.BOXKITE_MCP_CATALOG_RAW = " slack = mcp.slack.com , notion=mcp.notion.com ,"
        catalog = settings.BOXKITE_MCP_CATALOG
        assert catalog == {"slack": "mcp.slack.com", "notion": "mcp.notion.com"}
    finally:
        settings.BOXKITE_MCP_CATALOG_RAW = original
