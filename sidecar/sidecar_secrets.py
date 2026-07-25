"""Secrets-broker HTTP request path (docs/SECRETS-DESIGN.md §3): the
/http-request route plus the DNS-rebinding-safe destination validation and
in-process secret substitution/scrubbing it relies on.

Split out of the original monolithic ``main.py`` (GitHub issue #71) as a pure
mechanical refactor -- no behavior change. Session state
(``current_session``, ``_secret_value_cache``) and models remain owned by
``main`` and are referenced via ``main.<NAME>``; ``_get_secret_value`` and
``_resolve_and_validate_destination`` are called via ``main.`` because tests
monkeypatch them there. ``_socket`` is imported locally, but it is the same
module object as ``main._socket`` (module singletons), so a test that patches
``main._socket.getaddrinfo`` is observed here too.
"""

import asyncio
import ipaddress
import logging
import re as _re
import socket as _socket
import time as _time
from typing import Optional

from fastapi import APIRouter, HTTPException

import main

logger = logging.getLogger("sidecar")

router = APIRouter()


_SECRET_TOKEN_RE = _re.compile(r"\{\{secret:([^{}]+)\}\}")

# RFC 6598 shared address space (CGNAT), 100.64.0.0/10 -- not covered by
# ipaddress.is_private, but Alibaba Cloud's IMDS (100.100.100.200) lives
# here. Mirrors control_plane.host_safety's copy of this same constant.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")
# RFC 6052 NAT64 well-known prefix -- an IPv6 address here embeds an IPv4
# address in its low 32 bits (e.g. a metadata IP smuggled in IPv6 form).
_NAT64_NETWORK = ipaddress.ip_network("64:ff9b::/96")


def _embedded_ipv4(ip):
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _NAT64_NETWORK:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def _is_disallowed_destination_ip(ip_str: str) -> bool:
    """Mirrors control_plane.host_safety.is_disallowed_destination_ip so
    both layers (creation-time backstop, request-time real control) agree
    on what "private/link-local/loopback/metadata" means. See this module's
    `_resolve_and_validate_destination` for why the request-time version of
    this check is the one that actually matters."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(addr)
        if embedded is not None:
            return _is_disallowed_destination_ip(str(embedded))
    return (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_private
        or addr.is_unspecified
        or addr.is_reserved
        or addr.is_multicast
        or (isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT_NETWORK)
    )


async def _resolve_and_validate_destination(hostname: str) -> str:
    """Re-resolve `hostname` via DNS RIGHT NOW -- immediately before
    connecting, not merely at some earlier allowlist-check point -- reject
    if ANY resolved address is private/link-local/loopback/metadata, and
    return one validated address literal for the caller to connect to
    DIRECTLY (see `http_request`'s use of the `sni_hostname` extension to
    preserve TLS SNI/certificate-hostname verification while physically
    connecting to that literal IP rather than the hostname).

    This return-the-validated-IP step is what actually closes the classic
    TOCTOU DNS-rebinding gap docs/SECRETS-DESIGN.md §5 calls out as the
    concrete version of "an application-layer allowlist isn't the same as a
    network-layer one": if this function only returned pass/fail and the
    caller then handed httpx the *hostname* to connect with, httpx would
    perform its OWN, separate DNS lookup at connect time -- and a rebinding
    attacker (a DNS record with TTL=0 or similar) could return a safe
    address to this check and a private/metadata address moments later to
    httpx's independent lookup. Connecting to the literal IP this function
    already validated means that second, attacker-controllable lookup never
    happens at all.

    Raises HTTPException(403) if ANY resolved address is disallowed (not
    just the first one -- a hostname with a mix of public and private/
    metadata A/AAAA records is refused outright, never "pick the safe one
    and proceed") or if resolution fails.
    """
    loop = asyncio.get_event_loop()
    try:
        infos = await loop.run_in_executor(None, _socket.getaddrinfo, hostname, None)
    except _socket.gaierror as exc:
        raise HTTPException(status_code=403, detail=f"destination_not_allowed: DNS resolution failed for {hostname}: {exc}")

    if not infos:
        raise HTTPException(status_code=403, detail=f"destination_not_allowed: no addresses resolved for {hostname}")

    resolved_ips: list[str] = []
    for info in infos:
        ip_str = info[4][0]
        if _is_disallowed_destination_ip(ip_str):
            logger.warning(
                f"[http-request] Refusing destination {hostname} -> {ip_str} "
                "(private/link-local/loopback/metadata address)"
            )
            raise HTTPException(
                status_code=403,
                detail=f"destination_not_allowed: {hostname} resolves to a disallowed address",
            )
        resolved_ips.append(ip_str)

    return resolved_ips[0]


def _host_allowed_for_secret(host: str, allowed_hosts: list[str]) -> bool:
    host = host.lower()
    return any(host == allowed.lower().strip() for allowed in allowed_hosts)


async def _get_secret_value(name: str) -> Optional[str]:
    """Resolve one granted secret's real value, via the short in-memory TTL
    cache (docs/SECRETS-DESIGN.md §4) or a fresh call to the control
    plane's internal resolve endpoint. Returns None if the name isn't in
    this session's grant list, isn't configured, or resolution fails --
    the caller treats that as "substitution unavailable", never crashes the
    whole request over one bad secret reference."""
    if name not in (main.current_session.get("secret_names") or []):
        return None

    cached = main._secret_value_cache.get(name)
    if cached is not None:
        value, expires_at = cached
        if _time.monotonic() < expires_at:
            return value
        del main._secret_value_cache[name]

    base_url = main.current_session.get("secrets_control_plane_url")
    token = main.current_session.get("secret_capability_token")
    session_id = main.current_session.get("session_id")
    if not base_url or not token or not session_id:
        logger.error("[http-request] Secrets broker not configured for this session")
        return None

    import httpx as _httpx

    try:
        async with _httpx.AsyncClient(base_url=base_url, timeout=10) as client:
            response = await client.post(
                "/internal/secrets/resolve",
                json={"session_id": session_id, "secret_name": name},
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code != 200:
            logger.error(
                f"[http-request] Secret resolve failed for {name!r}: {response.status_code}"
            )
            return None
        value = response.json()["value"]
    except Exception as exc:
        logger.error(f"[http-request] Secret resolve error for {name!r}: {exc}")
        return None

    main._secret_value_cache[name] = (value, _time.monotonic() + main._SECRET_VALUE_CACHE_TTL_SECONDS)
    return value


async def _substitute_secrets(text: Optional[str]) -> tuple[Optional[str], dict[str, str]]:
    """Replace every literal `{{secret:name}}` reference in `text` with the
    real value. Returns (substituted_text, {name: value}) -- the caller uses
    the returned mapping to scrub these exact values back out of the
    response before it's ever handed back to the agent (see
    `_scrub_secret_values`). A referenced name that isn't resolvable is left
    as the literal token (never silently dropped/blanked, so a caller sees
    an unambiguous failure rather than a request that silently omits the
    credential)."""
    if not text:
        return text, {}

    used: dict[str, str] = {}

    names = set(_SECRET_TOKEN_RE.findall(text))
    for name in names:
        value = await main._get_secret_value(name)
        if value is not None:
            used[name] = value

    def _replace(match: "_re.Match[str]") -> str:
        name = match.group(1)
        return used.get(name, match.group(0))

    return _SECRET_TOKEN_RE.sub(_replace, text), used


def _scrub_secret_values(text: str, used_secrets: dict[str, str]) -> str:
    """Exact-value scrub -- the sidecar knows the literal secret values it
    just used and can do precise matching, a strictly stronger check than
    bash_tool.py's shape-based heuristic patterns (which this new route
    doesn't go through at all). Catches a destination echoing a credential
    back in an error body (e.g. "invalid key sk_live_abc...")."""
    for name, value in used_secrets.items():
        if value:
            text = text.replace(value, f"[REDACTED_SECRET:{name}]")
    return text


@router.post("/http-request", response_model=main.HttpRequestResponse)
async def http_request(req: main.HttpRequestRequest):
    """
    Secrets-broker HTTP request (docs/SECRETS-DESIGN.md §3). The SIDECAR
    itself -- never the sandboxed process -- builds and sends this request,
    substituting any `{{secret:name}}` reference in headers/body for the
    real value in-process before sending. No env var, no TLS interception,
    no CA injection -- the sidecar makes one ordinary outbound TLS
    connection itself, same as S3Backend/AzureBlobBackend already do for
    storage sync.

    Security-critical ordering, do not reorder:
    1. Parse the destination host from `url`.
    2. Determine which secret(s) `headers`/`body` actually reference.
    3. For each referenced secret, verify the destination host is on THAT
       secret's own allowed_hosts (never a union across secrets).
    4. Re-resolve the destination host via DNS right now and validate the
       resolved IP is not private/link-local/loopback/metadata -- this is
       the request-time DNS-rebinding check; the allowed_hosts check above
       is necessary but not sufficient on its own (see
       `_resolve_and_validate_destination`'s docstring).
    5. Only then substitute and send the real request.
    6. Scrub the exact secret values used out of the response before
       returning it.

    Does NOT follow redirects automatically -- a 3xx response is returned
    as-is, never silently re-validated-and-followed, since following a
    redirect to an unapproved host is a known SSRF bypass this route's
    allowlist can't retroactively cover.
    """
    import httpx as _httpx
    from urllib.parse import urlparse

    parsed = urlparse(req.url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(status_code=422, detail="url must be an absolute http(s) URL")
    hostname = parsed.hostname

    referenced_names = set(_SECRET_TOKEN_RE.findall(req.body or ""))
    for header_value in req.headers.values():
        referenced_names.update(_SECRET_TOKEN_RE.findall(header_value))

    secret_allowed_hosts = main.current_session.get("secret_allowed_hosts") or {}
    granted_names = set(main.current_session.get("secret_names") or [])
    for name in referenced_names:
        if name not in granted_names:
            # Never distinguish "not granted to me" from "doesn't exist" --
            # docs/SECRETS-DESIGN.md §3.
            raise HTTPException(
                status_code=404, detail="secret_not_referenced_by_session"
            )
        allowed_hosts = secret_allowed_hosts.get(name, [])
        if not _host_allowed_for_secret(hostname, allowed_hosts):
            raise HTTPException(status_code=403, detail="destination_not_allowed")

    # Request-time DNS-rebinding-safe check -- see docstring. Runs even when
    # no secret is referenced at all: this route is still an
    # arbitrary-outbound-request primitive and shouldn't become an SSRF
    # probe against internal infrastructure just because no credential was
    # attached to a given call. Returns a validated IP literal -- see below
    # for why the real connection is pinned to it directly.
    validated_ip = await main._resolve_and_validate_destination(hostname)

    substituted_headers: dict[str, str] = {}
    used_secrets: dict[str, str] = {}
    for key, value in req.headers.items():
        new_value, used = await _substitute_secrets(value)
        substituted_headers[key] = new_value or ""
        used_secrets.update(used)

    substituted_body, body_used = await _substitute_secrets(req.body)
    used_secrets.update(body_used)

    # Pin the actual connection to `validated_ip` -- the exact address
    # `_resolve_and_validate_destination` just validated -- rather than
    # handing httpx the hostname and letting it perform its own, separate,
    # attacker-influenceable DNS lookup at connect time. `Host` is set
    # explicitly (rewriting the URL's authority to an IP would otherwise
    # send that IP as the Host header) and the `sni_hostname` extension
    # tells httpx/httpcore to use the ORIGINAL hostname for TLS SNI and
    # certificate hostname verification, so HTTPS to a vhosted destination
    # still works and still validates the cert against the real hostname --
    # only the DNS lookup itself is what's pinned out of the attacker's
    # control.
    netloc_host = f"[{validated_ip}]" if ":" in validated_ip else validated_ip
    port_suffix = f":{parsed.port}" if parsed.port else ""
    pinned_url = parsed._replace(netloc=f"{netloc_host}{port_suffix}").geturl()

    request_headers = dict(substituted_headers)
    request_headers.setdefault("Host", hostname)

    try:
        async with _httpx.AsyncClient(timeout=req.timeout, follow_redirects=False) as client:
            outgoing = client.build_request(
                req.method.upper(),
                pinned_url,
                headers=request_headers,
                content=substituted_body,
            )
            outgoing.extensions["sni_hostname"] = hostname
            response = await client.send(outgoing)
    except _httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="destination_request_timed_out")
    except _httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"destination_request_failed: {exc}")

    max_response_bytes = 1 * 1024 * 1024
    raw_body = response.text
    truncated = False
    if len(raw_body) > max_response_bytes:
        raw_body = raw_body[:max_response_bytes]
        truncated = True

    scrubbed_body = _scrub_secret_values(raw_body, used_secrets)
    scrubbed_headers = {
        k: _scrub_secret_values(v, used_secrets) for k, v in response.headers.items()
    }

    logger.info(
        f"[http-request] method={req.method} host={hostname} status={response.status_code} "
        f"secrets_used={sorted(used_secrets.keys())}"
    )

    return main.HttpRequestResponse(
        status_code=response.status_code,
        headers=scrubbed_headers,
        body=scrubbed_body,
        truncated=truncated,
    )
