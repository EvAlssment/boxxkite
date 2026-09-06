"""Cross-SDK error taxonomy parity (GitHub issue #99).

Four hand-written SDKs, no codegen, and a documented history of drifting
apart: #131's review found sdk-rust shipped only accessors while the other
three got typed errors, and sdk-go classified `service_unavailable` by status
alone. Both were caught by a human reading four diffs. This is that check,
automated.

`specs/error-taxonomy.json` is the source of truth. sdk-python is verified by
importing and calling the real classifier; the other three are checked
statically against their source, since running node/go/cargo is not worth it
for what is fundamentally "does this file mention this string in the right
place". A static check that fails loudly on a missing code is still strictly
better than a human noticing months later.

Its limit, stated plainly: presence of a string is not proof of correct
behaviour. sdk-go mentioned `service_unavailable` while still failing to
classify it on a 4xx, and this file would not have caught that. Behavioural
parity lives in each SDK's own suite (sdk-go/errors_test.go,
sdk-rust/tests/error_taxonomy_test.rs, and the Python cases below, which do
execute the real classifier). This file's job is the coverage floor: no code
in the spec may be unknown to any SDK.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC = json.loads((REPO_ROOT / "specs" / "error-taxonomy.json").read_text())
CODES = SPEC["codes"]
SUFFIXES = [k for k in SPEC["suffix_rules"] if not k.startswith("$")]


def test_spec_is_self_consistent():
    """Every code maps to a declared class, and every class is reachable."""
    declared = set(SPEC["classes"])
    mapped = set(CODES.values())
    assert mapped <= declared, f"codes map to undeclared classes: {mapped - declared}"
    for suffix_class in (v for k, v in SPEC["suffix_rules"].items() if not k.startswith("$")):
        assert suffix_class in declared
    from_suffixes = {v for k, v in SPEC["suffix_rules"].items() if not k.startswith("$")}
    unreachable = declared - mapped - from_suffixes - {SPEC["fallbacks"]["status_5xx"]}
    assert not unreachable, f"classes no code can produce: {unreachable}"


# ── sdk-python: verified by executing the real classifier ────────────────


def _python_classifier():
    """Load sdk-python's classifier from the repo source, by path.

    Deliberately not a plain `import`. An older boxxkite_client may already be
    installed in the venv and cached in sys.modules from an earlier test, in
    which case a sys.path tweak is a silent no-op and this file would grade the
    installed copy instead of the source under review. Loading the file
    directly under a private module name makes the target unambiguous.
    """
    import importlib.util

    path = REPO_ROOT / "sdk-python" / "src" / "boxxkite_client" / "exceptions.py"
    spec = importlib.util.spec_from_file_location("_parity_exceptions", path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.api_error_type

    return api_error_type


@pytest.mark.parametrize("code,expected_class", sorted(CODES.items()))
def test_sdk_python_classifies_every_spec_code(code, expected_class):
    api_error_type = _python_classifier()
    cls = api_error_type(code, 400)
    # BoxxkiteEgressDeniedError -> egress_denied
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", cls.__name__.replace("Boxxkite", "").replace("Error", "")).lower()
    assert snake == expected_class, f"{code} -> {cls.__name__}, expected {expected_class}"


@pytest.mark.parametrize("suffix", SUFFIXES)
def test_sdk_python_applies_the_suffix_rules(suffix):
    api_error_type = _python_classifier()
    assert api_error_type(f"something{suffix}", 429).__name__ == "BoxxkiteQuotaExceededError"


def test_sdk_python_5xx_fallback():
    api_error_type = _python_classifier()
    assert api_error_type("brand_new_code", 503).__name__ == "BoxxkiteServiceUnavailableError"


# ── the other three: static source checks ────────────────────────────────

_SOURCES = {
    "sdk-js": Path("sdk-js/src/errors.ts"),
    "sdk-go": Path("sdk-go/client.go"),
    "sdk-rust": Path("sdk-rust/src/error.rs"),
}


@pytest.mark.parametrize("sdk,rel", sorted(_SOURCES.items()))
@pytest.mark.parametrize("code", sorted(CODES))
def test_every_sdk_handles_every_spec_code(sdk, rel, code):
    """A code in the spec that an SDK never mentions is drift, full stop."""
    source = (REPO_ROOT / rel).read_text()
    assert code in source, (
        f"{sdk} ({rel}) never mentions {code!r}. Either teach it the code or "
        f"remove the code from specs/error-taxonomy.json."
    )


@pytest.mark.parametrize("sdk,rel", sorted(_SOURCES.items()))
@pytest.mark.parametrize("suffix", SUFFIXES)
def test_every_sdk_applies_the_suffix_rules(sdk, rel, suffix):
    source = (REPO_ROOT / rel).read_text()
    assert suffix in source, f"{sdk} ({rel}) is missing the {suffix!r} quota rule"


@pytest.mark.parametrize("sdk,rel", sorted(_SOURCES.items()))
def test_every_sdk_has_the_5xx_fallback(sdk, rel):
    source = (REPO_ROOT / rel).read_text()
    assert re.search(r"(>=\s*500|statusCode >= 500|status >= 500)", source), (
        f"{sdk} ({rel}) has no >= 500 fallback, so an unrecognised 5xx would "
        f"not classify as service_unavailable"
    )
