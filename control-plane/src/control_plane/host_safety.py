"""Host/IP safety checks against private/link-local/loopback/cloud-metadata
addresses, used by two different features at two different times
(docs/SECRETS-DESIGN.md section 3/5, docs/WEBHOOKS-DESIGN.md section 8):

- `resolve_host_is_unsafe` is a best-effort, creation-time-only backstop --
  "a cheap, worthwhile, but non-sufficient first filter", not the real
  control. It runs once, at `POST /v1/secrets` or `POST /v1/webhooks` time,
  against whatever a hostname resolves to *right now*; DNS can point
  anywhere by the time a request is actually made.
- `resolve_and_validate_destination_ip` is the real, load-bearing,
  request-time control: re-resolve immediately before connecting and return
  the validated IP literal to connect to directly, never a bare hostname a
  caller would independently re-resolve. It mirrors the sidecar's own
  request-time check (`sidecar/sidecar_secrets.py`'s
  `_resolve_and_validate_destination`) exactly, including the
  IP-classification logic below, so every layer in this codebase agrees on
  what "private/link-local/metadata" means -- see that function's docstring
  for the DNS-rebinding rationale a creation-time-only check alone cannot
  close. `webhook_delivery.py` calls it on every delivery attempt (GitHub
  issue #148); the secrets broker's own use lives in the sidecar itself
  rather than here since the sidecar makes that outbound connection, not
  the control plane.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

# RFC 6598 "shared address space" (CGNAT), 100.64.0.0/10. Not covered by
# ipaddress.IPv4Address.is_private, but it's a real, actively-used
# cloud-metadata address: Alibaba Cloud's IMDS is 100.100.100.200, inside
# this block.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")

# RFC 6052 NAT64 well-known prefix. An IPv6 address in this range embeds an
# IPv4 address in its low 32 bits (e.g. `64:ff9b::a9fe:a9fe` embeds
# 169.254.169.254). `ipaddress` only unwraps the `::ffff:0:0/96`
# IPv4-mapped form via `.ipv4_mapped`; without this explicit check a
# metadata address reachable via a NAT64-enabled dual-stack cluster would
# bypass this deny-list entirely.
_NAT64_NETWORK = ipaddress.ip_network("64:ff9b::/96")


def _embedded_ipv4(ip: "ipaddress.IPv6Address"):
    """Returns the IPv4 address embedded in `ip` if it's IPv4-mapped
    (::ffff:0:0/96) or NAT64 (64:ff9b::/96), else None."""
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip in _NAT64_NETWORK:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_disallowed_destination_ip(ip_str: str) -> bool:
    """True if `ip_str` must never be a secrets-broker destination:
    loopback, link-local (which includes the 169.254.169.254 cloud-metadata
    address on AWS/GCP/DigitalOcean), RFC1918 private ranges, unspecified,
    reserved, multicast, or CGNAT/shared-address-space (100.64.0.0/10,
    covers Alibaba Cloud's IMDS). Also unwraps IPv4-mapped and
    NAT64-embedded IPv6 addresses and re-checks the embedded IPv4 address,
    so a metadata IP can't be smuggled through in IPv6 form. Mirrors
    deploy/network-policy.yaml's IMDS-blocking rationale, applied here at
    the application layer since the network layer can't enforce a
    per-secret, per-tenant allowlist at all (see docs/SECRETS-DESIGN.md
    section 5)."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        # Not a parseable IP at all -- treat as disallowed rather than
        # silently letting an unparseable value through.
        return True
    if isinstance(addr, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(addr)
        if embedded is not None:
            return is_disallowed_destination_ip(str(embedded))
    return (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_private
        or addr.is_unspecified
        or addr.is_reserved
        or addr.is_multicast
        or (isinstance(addr, ipaddress.IPv4Address) and addr in _CGNAT_NETWORK)
    )


# Hostnames that resolve to the cloud-metadata service via a well-known name
# rather than (or in addition to) the 169.254.169.254 literal -- rejected by
# name outright at creation time regardless of what they currently resolve
# to, since some of these are documented aliases some cloud DNS setups honor.
DISALLOWED_METADATA_HOSTNAMES = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "169.254.169.254",
    }
)


def hostname_is_obviously_unsafe(hostname: str) -> bool:
    """Cheap, name-only check -- catches the easy case before any DNS
    lookup. Not a substitute for `is_disallowed_destination_ip`."""
    return hostname.strip().lower() in DISALLOWED_METADATA_HOSTNAMES


def resolve_host_is_unsafe(hostname: str) -> bool:
    """Resolve `hostname` and check every returned address. Best-effort:
    resolution failures are treated as "cannot confirm safe", so callers
    should decide for themselves whether an unresolvable host at creation
    time is acceptable (this module doesn't reject on resolution failure by
    itself -- see routers/secrets.py for how it's actually used)."""
    if hostname_is_obviously_unsafe(hostname):
        return True
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    return any(is_disallowed_destination_ip(info[4][0]) for info in infos)


async def resolve_and_validate_destination_ip(hostname: str) -> str | None:
    """Re-resolve `hostname` via DNS RIGHT NOW -- immediately before
    connecting, not merely at some earlier registration-time check -- and
    return one validated IP literal for the caller to connect to DIRECTLY.
    Returns None if resolution fails or if ANY resolved address is
    private/link-local/loopback/metadata (never "pick the safe one and
    proceed" for a hostname with a mix of public and private/metadata
    records).

    This is the request-time half of the DNS-rebinding-safe pattern
    `sidecar/sidecar_secrets.py`'s `_resolve_and_validate_destination`
    already established for the secrets broker: a creation-time-only check
    (`resolve_host_is_unsafe` above) cannot close the TOCTOU gap between
    whenever it ran and the moment a connection is actually made, because a
    hostname that resolved to a public IP at check time can be repointed via
    DNS to `169.254.169.254` or an internal address before the next
    connection. Returning the validated IP -- rather than pass/fail alone --
    is what actually closes that gap: a caller that then connects to the
    literal IP this function validated never performs its own, separately
    attacker-influenceable DNS lookup at connect time. See
    `webhook_delivery.py`'s `_attempt_delivery` for the caller that pins its
    connection to this return value (GitHub issue #148).

    Runs the blocking `socket.getaddrinfo` call in an executor since this is
    called from async code with no synchronous fallback expected.
    """
    loop = asyncio.get_event_loop()
    try:
        infos = await loop.run_in_executor(None, socket.getaddrinfo, hostname, None)
    except socket.gaierror:
        return None
    if not infos:
        return None

    resolved_ips: list[str] = []
    for info in infos:
        ip_str = info[4][0]
        if is_disallowed_destination_ip(ip_str):
            return None
        resolved_ips.append(ip_str)
    return resolved_ips[0]
