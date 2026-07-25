"""Tests for the desktop-takeover resource-floor enforcement (GitHub issue
#184, docs/GUI-COMPUTER-USE-SCOPING.md): a full Xvfb + window manager +
x11vnc stack requires size='medium' or 'large', mirroring
tests/test_browser_resource_floor.py's own coverage shape for
_validate_browser_resource_floor.
"""

import pytest

from boxkite._manager_config import _validate_desktop_resource_floor


def test_validate_desktop_resource_floor_allows_small_when_desktop_disabled():
    _validate_desktop_resource_floor("small", False)  # must not raise


def test_validate_desktop_resource_floor_allows_medium_and_large_with_desktop_enabled():
    _validate_desktop_resource_floor("medium", True)  # must not raise
    _validate_desktop_resource_floor("large", True)  # must not raise


def test_validate_desktop_resource_floor_rejects_small_with_desktop_enabled():
    with pytest.raises(ValueError, match="desktop_enabled=True requires size='medium' or 'large'"):
        _validate_desktop_resource_floor("small", True)
