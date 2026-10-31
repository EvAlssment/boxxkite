"""Gymnasium environments backed by Boxxkite sandbox sessions."""

try:
    from .env import BoxxkiteEnv
except ModuleNotFoundError as exc:
    if exc.name != "gymnasium":
        raise
    raise ImportError(
        "boxxkite-gym requires its optional Gymnasium dependency; "
        "install boxxkite-gym[gymnasium]"
    ) from exc

__all__ = ["BoxxkiteEnv"]
