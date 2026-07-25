"""Read the ~/.boxkite/local.env file that `boxkite up` writes.

Shared by the CLI's config_store and by SandboxManager: in compose mode the
manager falls back to this file so a library user (or an example script)
doesn't have to re-export SIDECAR_AUTH_TOKEN / SIDECAR_URL into the environment
by hand after running `boxkite up`.

Deliberately stdlib-only and free of any boxkite imports so the core
SandboxManager can use it without pulling in the CLI package (typer + all the
command modules) at instantiation time.
"""

from __future__ import annotations

from pathlib import Path

CONFIG_DIR = Path.home() / ".boxkite"
LOCAL_ENV_FILE = CONFIG_DIR / "local.env"


def parse_local_env(path: Path) -> dict[str, str]:
    """Parse a KEY=value env file into a dict. A missing or unreadable file
    yields an empty dict (callers treat that as "nothing configured")."""
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text()
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def read_local_env_credentials(path: Path | None = None) -> tuple[str, str] | None:
    """Return `(sidecar_url, token)` from the local.env file, or None when
    either value is missing. Defaults to the standard ~/.boxkite/local.env."""
    values = parse_local_env(path if path is not None else LOCAL_ENV_FILE)
    token = values.get("SIDECAR_AUTH_TOKEN")
    sidecar_url = values.get("SIDECAR_URL")
    if not token or not sidecar_url:
        return None
    return sidecar_url, token
