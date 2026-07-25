"""Tests for command-whitelist enforcement, focused on the argument-position
command runners (env/xargs/timeout/nohup/nice/setsid/stdbuf/watch/flock/find).

Allowlisting a wrapper like `timeout` must not become a hole past the
allowlist: `timeout 1 rm -rf /` has to be blocked unless `rm` is also allowed.
These mirror the recursive treatment `ssh` already received.
"""

from __future__ import annotations

import pytest

from boxkite.command_whitelist import validate_command_whitelist


def _allowed(command, allow):
    ok, _ = validate_command_whitelist(command, allow)
    return ok


class TestBaselineUnchanged:
    def test_plain_allowed_command_passes(self):
        assert _allowed("grep foo", ["grep"]) is True

    def test_plain_disallowed_command_blocked(self):
        assert _allowed("rm -rf /", ["grep"]) is False

    def test_empty_allowlist_is_unrestricted(self):
        assert _allowed("rm -rf /", None) is True
        assert _allowed("rm -rf /", []) is True

    def test_ssh_remote_command_still_validated(self):
        assert _allowed("ssh host 'uptime'", ["ssh", "uptime"]) is True
        assert _allowed("ssh host 'rm -rf /'", ["ssh", "uptime"]) is False


# (command, allowlist) pairs where the wrapper is allowed but the program it
# launches is NOT — every one must be blocked.
_HIDDEN_COMMAND_CASES = [
    ("timeout 1 rm -rf /", ["timeout"]),
    ("timeout -s KILL 1 rm -rf /", ["timeout"]),
    ("env rm -rf /", ["env"]),
    ("env FOO=bar rm -rf /", ["env"]),
    ("env -S 'rm -rf /'", ["env"]),
    ("nohup rm -rf /", ["nohup"]),
    ("nice -n 5 rm -rf /", ["nice"]),
    ("nice -5 rm -rf /", ["nice"]),
    ("setsid rm -rf /", ["setsid"]),
    ("stdbuf -oL rm -rf /", ["stdbuf"]),
    ("stdbuf -o L rm -rf /", ["stdbuf"]),
    ("watch -n 2 rm -rf /", ["watch"]),
    ("xargs rm", ["xargs"]),
    ("xargs -n1 rm", ["xargs"]),
    ("flock /tmp/lock rm -rf /", ["flock"]),
    ("flock -x /tmp/lock -c 'rm -rf /'", ["flock"]),
]


@pytest.mark.parametrize("command,allow", _HIDDEN_COMMAND_CASES)
def test_wrapper_cannot_hide_disallowed_program(command, allow):
    assert _allowed(command, allow) is False


# (command, allowlist) pairs where both the wrapper AND the wrapped program are
# allowed — the composition must pass.
_LEGIT_CASES = [
    ("timeout 5 grep foo", ["timeout", "grep"]),
    ("timeout -s KILL 5 grep foo", ["timeout", "grep"]),
    ("env FOO=bar grep x", ["env", "grep"]),
    ("env -S 'grep x'", ["env", "grep"]),
    ("nohup grep x", ["nohup", "grep"]),
    ("nice -n 5 grep x", ["nice", "grep"]),
    ("setsid grep x", ["setsid", "grep"]),
    ("stdbuf -oL grep x", ["stdbuf", "grep"]),
    ("watch -n 2 grep x", ["watch", "grep"]),
    ("xargs grep x", ["xargs", "grep"]),
    ("flock /tmp/lock grep x", ["flock", "grep"]),
    ("flock -x /tmp/lock -c 'grep x'", ["flock", "grep"]),
    ("timeout 5 nice -n 1 grep x", ["timeout", "nice", "grep"]),
]


@pytest.mark.parametrize("command,allow", _LEGIT_CASES)
def test_wrapper_allows_wrapped_program_when_both_allowed(command, allow):
    assert _allowed(command, allow) is True


class TestWrapperEdgeCases:
    def test_wrapper_not_in_allowlist_blocked_even_if_inner_is(self):
        assert _allowed("timeout 1 grep foo", ["grep"]) is False

    def test_bare_wrapper_with_no_wrapped_program_allowed(self):
        assert _allowed("env", ["env"]) is True
        assert _allowed("xargs", ["xargs"]) is True

    def test_nested_wrappers_still_validate_innermost(self):
        assert _allowed("timeout 5 nice -n 1 rm x", ["timeout", "nice", "grep"]) is False


class TestFindExec:
    @pytest.mark.parametrize(
        "command",
        [
            r"find . -exec rm {} \;",
            "find . -exec rm {} +",
            r"find . -execdir rm {} \;",
            r"find . -ok rm {} \;",
        ],
    )
    def test_find_exec_actions_denied(self, command):
        # Denied even when the launched program is itself allowlisted.
        assert _allowed(command, ["find", "rm"]) is False

    def test_plain_find_search_allowed(self):
        assert _allowed("find . -name '*.py'", ["find"]) is True
