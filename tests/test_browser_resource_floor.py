"""Tests for the browser-tool resource-floor enforcement and the
mismatched-config warning added for GitHub issue #119's security-review
follow-up (docs/BROWSER-EXEC-DESIGN.md §4/§5): a headless Chromium process
requires size='medium' or 'large', and browser_enabled=True with
BOXKITE_BROWSER_NETWORK_POLICY_ENABLED off must warn rather than silently
fail closed with no trail to follow.
"""

import logging

import pytest

from boxkite._manager_config import (
    _validate_browser_resource_floor,
    _warn_if_browser_enabled_without_network_policy,
)
from boxkite.resource_config import size_at_least


def test_size_at_least_orders_small_medium_large():
    assert size_at_least("small", "small") is True
    assert size_at_least("medium", "small") is True
    assert size_at_least("large", "small") is True
    assert size_at_least("small", "medium") is False
    assert size_at_least("medium", "medium") is True
    assert size_at_least("large", "medium") is True
    assert size_at_least("small", "large") is False
    assert size_at_least("medium", "large") is False
    assert size_at_least("large", "large") is True


def test_validate_browser_resource_floor_allows_small_when_browser_disabled():
    _validate_browser_resource_floor("small", False)  # must not raise


def test_validate_browser_resource_floor_allows_medium_and_large_with_browser_enabled():
    _validate_browser_resource_floor("medium", True)  # must not raise
    _validate_browser_resource_floor("large", True)  # must not raise


def test_validate_browser_resource_floor_rejects_small_with_browser_enabled():
    with pytest.raises(ValueError, match="browser_enabled=True requires size='medium' or 'large'"):
        _validate_browser_resource_floor("small", True)


def test_warn_if_browser_enabled_without_network_policy_warns(caplog):
    with caplog.at_level(logging.WARNING):
        _warn_if_browser_enabled_without_network_policy("sess-1", True, False)
    assert any("BOXKITE_BROWSER_NETWORK_POLICY_ENABLED is not set" in r.message for r in caplog.records)
    assert any("sess-1" in r.message for r in caplog.records)


def test_warn_if_browser_enabled_without_network_policy_silent_when_policy_enabled(caplog):
    with caplog.at_level(logging.WARNING):
        _warn_if_browser_enabled_without_network_policy("sess-1", True, True)
    assert caplog.records == []


def test_warn_if_browser_enabled_without_network_policy_silent_when_browser_disabled(caplog):
    with caplog.at_level(logging.WARNING):
        _warn_if_browser_enabled_without_network_policy("sess-1", False, False)
    assert caplog.records == []
