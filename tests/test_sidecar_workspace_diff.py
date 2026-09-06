"""workspace_diff: snapshot-based "what changed since I last looked" (issue #71).

The behaviour worth pinning down is not "does it list files" -- it is that the
tool never lies by omission. A touched-but-unchanged file must not be reported
as modified, a pruned directory must not silently swallow a real change, and
every cap must announce itself in `notes` rather than returning a short list
that reads as "nothing else happened".
"""

import os

import pytest

import main as sidecar_main
import sidecar_workspace_diff as wsdiff


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(ws))
    wsdiff._reset_state_for_tests()
    yield ws
    wsdiff._reset_state_for_tests()


async def _diff(**kw):
    return await wsdiff.workspace_diff(sidecar_main.WorkspaceDiffRequest(**kw))


def _p(workspace, *parts):
    """Paths come back as the resolved absolute path under the workspace root,
    the same shape ls/glob/grep return -- which is "/workspace/..." only
    because WORKSPACE_DIR is literally /workspace in a real sandbox."""
    return os.path.join(os.path.realpath(str(workspace)), *parts)


# ── baseline ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_call_establishes_a_baseline_instead_of_listing_everything(workspace):
    """A first call must not dump the whole tree as "added" -- that is noise
    that says nothing about what the agent did."""
    (workspace / "existing.txt").write_text("already here\n")

    result = await _diff(path="/")

    assert result.baseline is True
    assert result.changes == []
    assert result.files_scanned == 1
    assert result.checkpoint
    assert any("baseline" in n for n in result.notes)


# ── the three change kinds ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reports_added_removed_and_modified_with_a_diff(workspace):
    (workspace / "keep.txt").write_text("unchanged\n")
    (workspace / "edit.txt").write_text("line one\nline two\n")
    (workspace / "gone.txt").write_text("delete me\n")
    await _diff(path="/")

    (workspace / "new.txt").write_text("brand new\n")
    (workspace / "edit.txt").write_text("line one\nline two CHANGED\n")
    (workspace / "gone.txt").unlink()

    result = await _diff(path="/")

    by_path = {c.path: c for c in result.changes}
    assert set(by_path) == {_p(workspace, "new.txt"), _p(workspace, "edit.txt"), _p(workspace, "gone.txt")}
    assert by_path[_p(workspace, "new.txt")].change == "added"
    assert by_path[_p(workspace, "gone.txt")].change == "removed"
    assert by_path[_p(workspace, "gone.txt")].size_delta_bytes == -len("delete me\n")

    modified = by_path[_p(workspace, "edit.txt")]
    assert modified.change == "modified"
    assert "-line two" in modified.diff
    assert "+line two CHANGED" in modified.diff
    # The unchanged file is the point of the whole exercise.
    assert _p(workspace, "keep.txt") not in by_path


@pytest.mark.asyncio
async def test_a_touched_but_unchanged_file_is_not_reported_as_modified(workspace):
    """mtime alone is a liar: `touch`, `cp -p` and a rewrite with identical
    bytes all move it without changing content. Content hashing is what makes
    this correct, so it gets its own test."""
    target = workspace / "same.txt"
    target.write_text("identical bytes\n")
    await _diff(path="/")

    st = os.stat(target)
    target.write_text("identical bytes\n")
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))

    result = await _diff(path="/")

    assert result.changes == []


@pytest.mark.asyncio
async def test_detects_a_change_made_between_two_calls(workspace):
    """The gap watch_directory cannot see. This is the reason the tool exists,
    so it is asserted directly rather than implied by the tests above."""
    (workspace / "built.txt").write_text("v1\n")
    first = await _diff(path="/")

    # No watch is open here. A background build writes anyway.
    (workspace / "built.txt").write_text("v2\n")

    result = await _diff(path="/", checkpoint=first.checkpoint)

    assert [c.path for c in result.changes] == [_p(workspace, "built.txt")]


# ── checkpoints ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_explicit_checkpoint_compares_against_that_point_not_the_latest(workspace):
    (workspace / "a.txt").write_text("1\n")
    first = await _diff(path="/")

    (workspace / "b.txt").write_text("2\n")
    await _diff(path="/")

    (workspace / "c.txt").write_text("3\n")
    # Against the first checkpoint, both b and c are new.
    from_first = await _diff(path="/", checkpoint=first.checkpoint)
    assert {c.path for c in from_first.changes} == {_p(workspace, "b.txt"), _p(workspace, "c.txt")}


@pytest.mark.asyncio
async def test_an_unknown_checkpoint_is_an_error_not_a_silent_rebaseline(workspace):
    """Silently re-baselining would report "no changes" for a checkpoint that
    expired, which is indistinguishable from "nothing changed" and wrong."""
    from fastapi import HTTPException

    (workspace / "a.txt").write_text("1\n")
    await _diff(path="/")

    with pytest.raises(HTTPException) as exc:
        await _diff(path="/", checkpoint="wsdiff-doesnotexist")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_snapshot_store_is_bounded_and_evicts_oldest_first(workspace):
    (workspace / "a.txt").write_text("1\n")
    tokens = []
    for _ in range(wsdiff.WORKSPACE_DIFF_MAX_SNAPSHOTS + 3):
        tokens.append((await _diff(path="/")).checkpoint)

    assert len(wsdiff._snapshots) == wsdiff.WORKSPACE_DIFF_MAX_SNAPSHOTS
    assert tokens[0] not in wsdiff._snapshots
    assert tokens[-1] in wsdiff._snapshots
    # Eviction must not orphan the "latest for this path" pointer.
    assert wsdiff._latest_by_path[os.path.realpath(str(workspace))] == tokens[-1]


# ── pruning and caps ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_excludes_are_pruned(workspace):
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("print(1)\n")
    (workspace / "node_modules").mkdir()
    (workspace / ".git").mkdir()
    await _diff(path="/")

    (workspace / "node_modules" / "junk.js").write_text("x" * 100)
    (workspace / ".git" / "objects").write_text("deadbeef")
    (workspace / "src" / "app.py").write_text("print(2)\n")

    result = await _diff(path="/")

    assert [c.path for c in result.changes] == [_p(workspace, "src", "app.py")]


@pytest.mark.asyncio
async def test_a_caller_supplied_exclude_is_honoured(workspace):
    (workspace / "logs").mkdir()
    (workspace / "logs" / "run.log").write_text("start\n")
    (workspace / "keep.txt").write_text("a\n")
    await _diff(path="/", exclude=["logs"])

    (workspace / "logs" / "run.log").write_text("start\nmore\n")
    (workspace / "keep.txt").write_text("b\n")

    result = await _diff(path="/", exclude=["logs"])
    assert [c.path for c in result.changes] == [_p(workspace, "keep.txt")]


@pytest.mark.asyncio
async def test_binary_files_are_reported_without_a_diff(workspace):
    (workspace / "blob.bin").write_bytes(b"\x00\x01\x02")
    await _diff(path="/")
    (workspace / "blob.bin").write_bytes(b"\x00\x01\x02\x03\x04")

    result = await _diff(path="/")

    entry = result.changes[0]
    assert entry.binary is True
    assert entry.diff is None
    assert entry.diff_omitted_reason == "binary file"
    assert entry.size_delta_bytes == 2


@pytest.mark.asyncio
async def test_a_file_too_large_to_retain_is_still_reported_with_a_stated_reason(
    workspace, monkeypatch
):
    """The failure mode to avoid is a modified file that shows no diff and no
    explanation, which reads as "nothing really changed"."""
    monkeypatch.setattr(wsdiff, "WORKSPACE_DIFF_MAX_FILE_CONTENT_BYTES", 16)

    big = workspace / "big.txt"
    big.write_text("x" * 100)
    await _diff(path="/")
    big.write_text("y" * 200)

    result = await _diff(path="/")

    entry = result.changes[0]
    assert entry.change == "modified"
    assert entry.diff is None
    assert "too large" in entry.diff_omitted_reason


@pytest.mark.asyncio
async def test_hitting_the_file_cap_sets_truncated_and_says_so(workspace, monkeypatch):
    monkeypatch.setattr(wsdiff, "WORKSPACE_DIFF_MAX_FILES", 3)
    for i in range(10):
        (workspace / f"f{i}.txt").write_text(str(i))

    result = await _diff(path="/")

    assert result.truncated is True
    assert any("partial" in n for n in result.notes)


@pytest.mark.asyncio
async def test_a_long_diff_is_truncated_with_a_visible_marker(workspace):
    target = workspace / "long.txt"
    target.write_text("".join(f"line {i}\n" for i in range(500)))
    await _diff(path="/")
    target.write_text("".join(f"CHANGED {i}\n" for i in range(500)))

    result = await _diff(path="/", max_diff_bytes=1024)

    diff = result.changes[0].diff
    assert "diff truncated at" in diff


@pytest.mark.asyncio
async def test_include_diffs_false_still_reports_the_change(workspace):
    (workspace / "a.txt").write_text("1\n")
    await _diff(path="/")
    (workspace / "a.txt").write_text("2\n")

    result = await _diff(path="/", include_diffs=False)

    assert result.changes[0].change == "modified"
    assert result.changes[0].diff is None


# ── containment ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_symlinks_are_never_followed(workspace, tmp_path):
    """An agent can plant a symlink via /exec. Following one would both leak
    content from outside the sandbox roots and report phantom changes."""
    secret = tmp_path / "outside_secret.txt"
    secret.write_text("do not read me\n")
    (workspace / "innocent.txt").write_text("fine\n")
    await _diff(path="/")

    os.symlink(str(secret), str(workspace / "escape.txt"))
    secret.write_text("changed out of band\n")

    result = await _diff(path="/")

    assert [c.path for c in result.changes] == []


@pytest.mark.asyncio
async def test_an_entry_failing_the_containment_check_is_skipped_silently(
    workspace, monkeypatch
):
    """The containment re-check is defence in depth behind the symlink guard
    above, so a symlink cannot reach it -- it is driven directly instead.

    Two guarantees. The entry's content is never read, and the walk does not
    raise: raising mid-walk would turn "did this path resolve outside the
    allowed roots" into an observable side channel, which is exactly what
    _is_path_contained's own docstring warns about.

    It surfaces as "removed" rather than vanishing, because a file dropped
    from the new snapshot while present in the old one is, from the scan's
    point of view, gone. That is the honest report -- and notably it carries
    no content, which is the part that matters.
    """
    (workspace / "a.txt").write_text("1\n")
    (workspace / "b.txt").write_text("2\n")
    await _diff(path="/")

    (workspace / "a.txt").write_text("changed\n")
    (workspace / "b.txt").write_text("also changed\n")

    real_check = sidecar_main._is_path_contained
    monkeypatch.setattr(
        sidecar_main,
        "_is_path_contained",
        lambda path, roots: False if path.endswith("a.txt") else real_check(path, roots),
    )

    result = await _diff(path="/")

    by_path = {c.path: c for c in result.changes}
    assert by_path[_p(workspace, "a.txt")].change == "removed"
    assert by_path[_p(workspace, "a.txt")].diff is None
    assert by_path[_p(workspace, "b.txt")].change == "modified"


@pytest.mark.asyncio
async def test_a_path_outside_the_allowed_roots_is_rejected(workspace):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await _diff(path="/etc")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_a_flood_of_changes_is_capped_and_the_remainder_is_counted(
    workspace, monkeypatch
):
    """A dependency install or an rm -rf changes thousands of files at once.
    Returning all of them spends the agent's whole context on a list it cannot
    act on; returning some of them silently is worse."""
    monkeypatch.setattr(wsdiff, "WORKSPACE_DIFF_MAX_CHANGES", 5)
    await _diff(path="/")

    for i in range(20):
        (workspace / f"f{i}.txt").write_text(str(i))

    result = await _diff(path="/")

    assert len(result.changes) == 5
    assert any("15 further changed file(s) not listed" in n for n in result.notes)
