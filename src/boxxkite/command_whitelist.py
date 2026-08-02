"""
Command Whitelist - token-aware allowlist enforcement for bash_tool

When an agent has `sandbox_allowed_commands` configured (non-empty list),
bash_tool switches from blocklist mode to whitelist mode: every command
position in the shell string must resolve to an allowed program — and, for
commands with argument rules, the arguments must satisfy those rules too.

Each entry in `sandbox_allowed_commands` is either:

  1. A plain string — the program name, with any arguments allowed:
        "grep"

  2. An object with argument-level constraints:
        {
          "command": "curl",
          "args_allow": ["https?://(localhost|sidecar)(:\\d+)?/"],
          "args_deny":  ["-X\\s*POST", "(^|\\s)(-d|--data)\\b"]
        }
     - args_allow: if present, the joined argument string must match at
       least one pattern (e.g. an allowed host/URL).
     - args_deny: the argument string must match none (e.g. block POST).
     Patterns are Python regexes, matched case-insensitively with re.search.

Multiple entries may share a command name; the command position passes if
ANY entry for that program accepts it (OR semantics). A plain-string entry
is an unconstrained rule, so listing both "curl" and a curl object makes
the object's restrictions moot.

Unlike the legacy substring blocklist in preset_packages.py (which had to be
mostly disabled due to false positives on document content), this parser is
shell-token-aware:

- Splits the command into simple commands on |, ||, &&, ;, & and newlines
- Resolves the program name (basename, case-insensitive) of each position
- Skips leading VAR=value environment assignments
- ssh gets special handling: the remote command string is extracted and
  recursively validated, so `ssh host 'uptime'` passes with ["ssh", "uptime"]
  but `ssh host 'rm -rf /'` is blocked. Interactive ssh (no remote command)
  is blocked because it could run anything. Any args_allow/args_deny on an
  ssh rule also apply to the full ssh argument string (e.g. restrict the
  destination host).
- Argument-position command runners get the same recursive treatment as ssh:
  env, xargs, timeout, nohup, nice, setsid, stdbuf, watch and flock all launch
  a program named in their arguments, so allowlisting one of them would
  otherwise be a hole straight past the allowlist (`timeout 1 rm -rf /` with
  only "timeout" allowed). When such a wrapper is allowlisted, its option
  prefix is skipped and the wrapped command is extracted and validated against
  the same allowlist, so `timeout 5 grep foo` passes only if `grep` is also
  allowed. `find` with an -exec/-execdir/-ok/-okdir action is denied outright
  in whitelist mode (its escaped-semicolon / `+` command terminator makes
  reliable extraction fragile, so it fails safe).

Constructs that can smuggle hidden commands are rejected outright in
whitelist mode: command substitution ($(...) and backticks), process
substitution (<(...)), and heredocs (<<). This fails safe — a legitimate
command using these will be blocked, but no disallowed command can hide.
"""

import logging
import posixpath
import re
import shlex
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Redirection operators — the token following them is a file target, not a command
_REDIRECT_OPERATORS = {">", ">>", "<", ">&", "<&", "<>", ">|", "&>", "&>>"}

# Characters shlex emits as punctuation runs; any token made only of these
# (that isn't a redirect) starts a new command position (|, ||, &&, ;, |&, …)
_PUNCTUATION_CHARS = set("();<>|&")


def _is_separator(token: str) -> bool:
    """True for shell operator tokens that start a new command position."""
    if token in _REDIRECT_OPERATORS:
        return False
    return bool(token) and all(ch in _PUNCTUATION_CHARS for ch in token)


def _is_redirect(token: str) -> bool:
    """True for redirect operators whose following token is a file target."""
    return token in _REDIRECT_OPERATORS

# ssh options that consume a value argument (OpenSSH client)
_SSH_OPTIONS_WITH_VALUE = {
    "-b", "-B", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J", "-L", "-l",
    "-m", "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w",
}

# Substrings that can hide commands from token-level inspection
_FORBIDDEN_CONSTRUCTS = [
    ("$(", "command substitution `$(...)`"),
    ("`", "command substitution with backticks"),
    ("<<", "heredoc input (`<<`)"),
    ("<(", "process substitution `<(...)`"),
    (">(", "process substitution `>(...)`"),
]


class _CommandRule:
    """One normalized allow-entry: a program name + optional arg regexes."""

    __slots__ = ("command", "args_allow", "args_deny")

    def __init__(self, command: str, args_allow, args_deny):
        self.command = command
        self.args_allow = args_allow  # list[compiled regex]
        self.args_deny = args_deny    # list[compiled regex]

    @property
    def constrained(self) -> bool:
        return bool(self.args_allow or self.args_deny)

    def check_args(self, arg_string: str) -> tuple[bool, str]:
        """Return (ok, reason) for this rule against a command's arg string."""
        if self.args_allow and not any(p.search(arg_string) for p in self.args_allow):
            return False, (
                f"its arguments do not match any allowed pattern for "
                f"'{self.command}'"
            )
        for pat in self.args_deny:
            if pat.search(arg_string):
                return False, (
                    f"its arguments match a denied pattern for "
                    f"'{self.command}' (/{pat.pattern}/)"
                )
        return True, ""


def get_allowed_commands_message(rules_or_commands: Any) -> str:
    """Human-readable list of allowed commands for error messages."""
    names = sorted(_rule_command_names(rules_or_commands))
    return (
        "This agent runs in command-whitelist mode. Only the following "
        "commands are allowed:\n  " + ", ".join(names)
    )


def _rule_command_names(rules_or_commands: Any) -> set[str]:
    names: set[str] = set()
    # Accept either the raw config list or a normalized {name: [rules]} map.
    if isinstance(rules_or_commands, dict):
        return set(rules_or_commands.keys())
    for entry in rules_or_commands or []:
        if isinstance(entry, str):
            if entry.strip():
                names.add(entry.strip().lower())
        elif isinstance(entry, dict):
            cmd = entry.get("command")
            if isinstance(cmd, str) and cmd.strip():
                names.add(cmd.strip().lower())
    return names


def _compile_patterns(patterns) -> list:
    compiled = []
    for pat in patterns or []:
        if not isinstance(pat, str) or not pat:
            continue
        try:
            compiled.append(re.compile(pat, re.IGNORECASE))
        except re.error as exc:
            logger.warning(
                "[command_whitelist] Skipping invalid arg pattern %r: %s",
                pat, exc,
            )
    return compiled


def _normalize_rules(allowed_commands) -> dict[str, list[_CommandRule]]:
    """Build a {program_name: [rule, ...]} map from the raw config list."""
    rules: dict[str, list[_CommandRule]] = {}
    for entry in allowed_commands or []:
        if isinstance(entry, str):
            name = entry.strip().lower()
            if not name:
                continue
            rules.setdefault(name, []).append(_CommandRule(name, [], []))
        elif isinstance(entry, dict):
            cmd = entry.get("command")
            if not isinstance(cmd, str) or not cmd.strip():
                continue
            name = cmd.strip().lower()
            rule = _CommandRule(
                name,
                _compile_patterns(entry.get("args_allow")),
                _compile_patterns(entry.get("args_deny")),
            )
            rules.setdefault(name, []).append(rule)
    return rules


def _program_name(token: str) -> str:
    """Resolve a command token to a comparable program name."""
    return posixpath.basename(token).lower()


def _is_env_assignment(token: str) -> bool:
    """True for leading VAR=value tokens before the program name."""
    if "=" not in token:
        return False
    name = token.split("=", 1)[0]
    return name.isidentifier()


def _tokenize(command: str) -> Optional[list[str]]:
    """
    Tokenize a shell string keeping operators as separate tokens.

    Returns None if the string cannot be tokenized (unbalanced quotes etc.) —
    callers must treat that as blocked.
    """
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        return None


def _check_command_position(
    program: str,
    arg_tokens: list[str],
    rules: dict[str, list[_CommandRule]],
) -> tuple[bool, str]:
    """
    Validate a single simple command's program + args against the rules.

    Passes if ANY rule for the program accepts the argument string.
    """
    program_rules = rules.get(program)
    if not program_rules:
        return False, (
            f"Blocked: '{program}' is not in this agent's allowed commands.\n\n"
            f"{get_allowed_commands_message(rules)}"
        )

    arg_string = " ".join(arg_tokens)
    last_reason = ""
    for rule in program_rules:
        ok, reason = rule.check_args(arg_string)
        if ok:
            return True, ""
        last_reason = reason

    return False, (
        f"Blocked: '{program}' is allowed but {last_reason}.\n\n"
        f"{get_allowed_commands_message(rules)}"
    )


def _validate_ssh_command(
    tokens: list[str],
    rules: dict[str, list[_CommandRule]],
    depth: int,
) -> tuple[bool, str]:
    """
    Validate the arguments of a single ssh invocation.

    `tokens` are the arguments after the `ssh` token (up to the next shell
    operator). Any args_allow/args_deny on an ssh rule apply to the full ssh
    argument string; the remote command is then extracted and recursively
    validated against the same allowlist.
    """
    # Apply ssh rule arg constraints (e.g. allowed destination host) first.
    ok, reason = _check_command_position("ssh", tokens, rules)
    if not ok:
        return ok, reason

    i = 0
    destination = None
    while i < len(tokens):
        tok = tokens[i]
        if destination is None and tok.startswith("-"):
            if tok in _SSH_OPTIONS_WITH_VALUE:
                i += 2  # option + its value
            else:
                i += 1  # boolean flag (or -oKey=Value combined form)
            continue
        if destination is None:
            destination = tok
            i += 1
            continue
        break  # first token after destination starts the remote command
    remote_tokens = tokens[i:]

    if destination is None:
        return False, "ssh command has no destination host."
    if not remote_tokens:
        return False, (
            "Interactive ssh sessions are not allowed in whitelist mode. "
            "Pass an explicit remote command, e.g. ssh host 'uptime'."
        )

    # The remote command may be a single quoted string or loose tokens;
    # either way validate it as a shell command against the same allowlist.
    remote_command = " ".join(remote_tokens)
    return _validate(remote_command, rules, depth + 1)


# find actions that run an arbitrary program; see the deny rationale in
# _validate_command_runner.
_FIND_EXEC_ACTIONS = {"-exec", "-execdir", "-ok", "-okdir"}


def _extract_wrapped_command(
    tokens: list[str],
    *,
    value_opts: set[str],
    command_opts: frozenset = frozenset(),
    skip_positionals: int = 0,
    skip_assignments: bool = False,
) -> list[str]:
    """
    Locate the program a command-runner wrapper will execute.

    Skips the wrapper's option prefix, then returns the remaining tokens
    joined as a shell command string to recursively validate.

    - value_opts: options that consume the following token as a value
      (e.g. timeout's -s/-k). Their combined form (-n2) and --long=value form
      are skipped as single tokens too.
    - command_opts: options whose value is itself a command string to validate
      instead of a wrapped program (e.g. flock's -c, env's -S/--split-string).
    - skip_positionals: fixed positional args before the command
      (e.g. timeout's DURATION).
    - skip_assignments: skip leading VAR=value tokens (env).

    Returns a list of shell command strings to validate, or [] when the wrapper
    runs no wrapped program (e.g. bare `env`). Best-effort and fail-safe: an
    unrecognized option is skipped as a boolean flag, which can only ever cause
    an over-block downstream, never let a command slip through unvalidated.
    """
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if skip_assignments and _is_env_assignment(tok):
            i += 1
            continue
        if tok == "--":
            i += 1
            break
        if tok in command_opts:
            return [tokens[i + 1]] if i + 1 < n else []
        if tok.startswith("--") and "=" in tok:
            if tok.split("=", 1)[0] in command_opts:
                return [tok.split("=", 1)[1]]
            i += 1
            continue
        if tok in value_opts:
            i += 2
            continue
        if tok.startswith("-") and tok != "-":
            # A combined short command-opt (-c'...' / -S'...') carries its
            # command inline; everything else is an option token to skip.
            for copt in command_opts:
                if len(copt) == 2 and tok.startswith(copt):
                    return [tok[2:]]
            i += 1
            continue
        break  # first bare token: a positional or the wrapped program
    i += skip_positionals
    rest = tokens[i:]
    return [" ".join(rest)] if rest else []


def _extract_flock(tokens: list[str]) -> list[str]:
    """flock's -c/--command carries a command string and may appear after the
    lock FILE, so scan for it anywhere before falling back to prefix-skipping
    (options, then the single lock-FILE positional, then the wrapped command)."""
    n = len(tokens)
    for idx, tok in enumerate(tokens):
        if tok in ("-c", "--command"):
            return [tokens[idx + 1]] if idx + 1 < n else []
        if tok.startswith("--command="):
            return [tok.split("=", 1)[1]]
        if tok.startswith("-c") and len(tok) > 2 and not tok.startswith("--"):
            return [tok[2:]]
    return _extract_wrapped_command(
        tokens,
        value_opts={"-w", "--wait", "--timeout", "-E", "--conflict-exit-code"},
        skip_positionals=1,
    )


# Wrappers that run a program named in an argument position. Each maps to an
# extractor returning the wrapped command string(s) to recursively validate.
# ssh is handled separately (its arg grammar differs); find is denied on -exec.
_COMMAND_RUNNERS = {
    "nohup": lambda t: _extract_wrapped_command(t, value_opts=set()),
    "setsid": lambda t: _extract_wrapped_command(t, value_opts=set()),
    "nice": lambda t: _extract_wrapped_command(t, value_opts={"-n", "--adjustment"}),
    "stdbuf": lambda t: _extract_wrapped_command(
        t, value_opts={"-i", "-o", "-e", "--input", "--output", "--error"}
    ),
    "timeout": lambda t: _extract_wrapped_command(
        t,
        value_opts={"-s", "--signal", "-k", "--kill-after"},
        skip_positionals=1,
    ),
    "env": lambda t: _extract_wrapped_command(
        t,
        value_opts={"-u", "--unset", "-C", "--chdir", "-P", "--argv0"},
        command_opts=frozenset({"-S", "--split-string"}),
        skip_assignments=True,
    ),
    "xargs": lambda t: _extract_wrapped_command(
        t,
        value_opts={
            "-I", "-i", "-n", "-L", "-P", "-s", "-d", "-E", "-a",
            "--replace", "--max-args", "--max-procs", "--max-chars",
            "--delimiter", "--arg-file", "--eof", "--max-lines",
        },
    ),
    "watch": lambda t: _extract_wrapped_command(t, value_opts={"-n", "--interval"}),
    "flock": _extract_flock,
}


def _validate_command_runner(
    program: str,
    arg_tokens: list[str],
    rules: dict[str, list[_CommandRule]],
    depth: int,
) -> tuple[bool, str]:
    """Validate an allowlisted command-runner wrapper and its wrapped command.

    Mirrors ssh: the wrapper's own args_allow/args_deny are applied first, then
    the program it would launch is extracted and recursively validated.
    """
    ok, reason = _check_command_position(program, arg_tokens, rules)
    if not ok:
        return ok, reason

    if program == "find":
        if any(tok in _FIND_EXEC_ACTIONS for tok in arg_tokens):
            return False, (
                "Blocked: 'find' with -exec/-execdir/-ok/-okdir can launch "
                "arbitrary programs and is not allowed in command-whitelist "
                "mode.\n\n" + get_allowed_commands_message(rules)
            )
        return True, ""  # a plain search launches no other program

    for inner in _COMMAND_RUNNERS[program](arg_tokens):
        if not inner.strip():
            continue
        ok, msg = _validate(inner, rules, depth + 1)
        if not ok:
            return ok, msg
    return True, ""


def _validate(
    command: str,
    rules: dict[str, list[_CommandRule]],
    depth: int,
) -> tuple[bool, str]:
    if depth > 3:
        return False, "Command nesting too deep to validate."

    for construct, label in _FORBIDDEN_CONSTRUCTS:
        if construct in command:
            return False, (
                f"Blocked: {label} is not allowed in command-whitelist mode "
                "because it can hide commands from validation.\n\n"
                f"{get_allowed_commands_message(rules)}"
            )

    # Newlines separate commands just like `;` — validate each line.
    lines = [line for line in command.splitlines() if line.strip()]
    if len(lines) > 1:
        for line in lines:
            ok, msg = _validate(line, rules, depth)
            if not ok:
                return ok, msg
        return True, ""

    tokens = _tokenize(command)
    if tokens is None:
        return False, (
            "Command could not be parsed (unbalanced quotes?) and is "
            "blocked in command-whitelist mode."
        )

    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]

        if _is_separator(tok):
            i += 1
            continue

        if _is_redirect(tok):
            i += 2  # skip operator + its file target
            continue

        # Skip leading VAR=value assignments before the program name.
        if _is_env_assignment(tok):
            i += 1
            continue

        # tok is the program for this simple command. Collect its args up to
        # the next shell operator (ignoring redirect target tokens).
        program = _program_name(tok)
        j = i + 1
        arg_tokens: list[str] = []
        while j < n and not _is_separator(tokens[j]):
            if _is_redirect(tokens[j]):
                j += 2  # skip redirect + target
                continue
            arg_tokens.append(tokens[j])
            j += 1

        if program == "ssh" and "ssh" in rules:
            ok, msg = _validate_ssh_command(arg_tokens, rules, depth)
        elif (program == "find" or program in _COMMAND_RUNNERS) and program in rules:
            ok, msg = _validate_command_runner(program, arg_tokens, rules, depth)
        else:
            ok, msg = _check_command_position(program, arg_tokens, rules)
        if not ok:
            return ok, msg

        i = j

    return True, ""


def validate_command_whitelist(
    command: str,
    allowed_commands: Optional[list],
) -> tuple[bool, str]:
    """
    Validate a bash_tool command against an agent's command allowlist.

    Args:
        command: The shell command string the agent wants to run
        allowed_commands: List of allow-entries — each a program name string
            (e.g. "ssh") or an object {command, args_allow?, args_deny?}.
            None or empty disables whitelist mode (returns allowed).

    Returns:
        Tuple of (is_allowed, error_message). error_message is agent-facing
        and lists the allowed commands and the reason for any block.
    """
    if not allowed_commands:
        return True, ""

    rules = _normalize_rules(allowed_commands)
    if not rules:
        return True, ""

    if not command or not command.strip():
        return False, "Error: Empty command provided"

    return _validate(command.strip(), rules, depth=0)
