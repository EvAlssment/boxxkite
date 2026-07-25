"""Tests for Low/Medium: no size cap on /file-create and /str-replace payloads.

`FileCreateRequest.content` and `StrReplaceRequest.old_str`/`new_str` had no
`max_length`, so an arbitrarily large payload would be accepted and written
to disk / held in memory. Both fields now cap at FILE_CONTENT_MAX_LENGTH,
kept in sync with the control-plane's own SANDBOX_FILE_CONTENT_MAX_LENGTH.
"""

import pytest
from pydantic import ValidationError

import main as sidecar_main


def test_file_create_request_rejects_oversized_content():
    with pytest.raises(ValidationError):
        sidecar_main.FileCreateRequest(
            path="/workspace/big.txt",
            content="x" * (sidecar_main.FILE_CONTENT_MAX_LENGTH + 1),
        )


def test_file_create_request_accepts_content_at_the_cap():
    req = sidecar_main.FileCreateRequest(
        path="/workspace/big.txt",
        content="x" * sidecar_main.FILE_CONTENT_MAX_LENGTH,
    )
    assert len(req.content) == sidecar_main.FILE_CONTENT_MAX_LENGTH


def test_str_replace_request_rejects_oversized_old_str():
    with pytest.raises(ValidationError):
        sidecar_main.StrReplaceRequest(
            path="/workspace/config.py",
            old_str="x" * (sidecar_main.FILE_CONTENT_MAX_LENGTH + 1),
            new_str="y",
        )


def test_str_replace_request_rejects_oversized_new_str():
    with pytest.raises(ValidationError):
        sidecar_main.StrReplaceRequest(
            path="/workspace/config.py",
            old_str="x",
            new_str="y" * (sidecar_main.FILE_CONTENT_MAX_LENGTH + 1),
        )
