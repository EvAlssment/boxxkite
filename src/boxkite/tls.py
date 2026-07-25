"""Per-pod self-signed TLS for manager-to-sidecar transport.

See docs/SIDECAR-TRANSPORT-TLS-DESIGN.md for the full design (Option B:
per-pod self-signed cert, pinned by the manager -- no CA, no service mesh
required by default).

SECURITY CONTEXT: `SandboxManager`/`WarmPoolManager` talk plaintext HTTP to
the sidecar's :8080 today, which puts the `X-Sidecar-Auth-Token` header
(see sidecar_auth.py) and every exec command/file body on the wire in
cleartext -- readable by a compromised co-located pod, a misconfigured/
widened NetworkPolicy, or a compromised node. This module closes that by
generating a fresh, short-lived, self-signed TLS keypair alongside the
existing per-pod sidecar auth token (same point in `_create_pod()`, same
per-pod Secret via `sidecar_auth_secret_name()` -- see manager.py), and
gives the manager a way to pin its HTTP client's trust to that *exact* cert
instead of validating against a public CA.

DEVIATION FROM THE DESIGN DOC (documented, not silent): the design doc
calls for an IP-SAN cert matching the pod's IP, generated at the same point
`generate_sidecar_auth_token()` is called in `_create_pod()`. That point is
*before* pod creation, and a pod's IP is only assigned by the CNI once the
pod is actually scheduled and running (`_wait_for_pod_ready()`, which runs
strictly after this cert is generated and baked into the pod's Secret/
volume mount) -- there is no way to know the pod's IP at cert-generation
time. Since a Secret referenced by a pod's volumeMount must exist before
`create_namespaced_pod` is called, the cert cannot be regenerated
after the IP is known without either delaying pod creation or restarting
the sidecar mid-boot. To resolve this without an IP SAN, `build_pinned_ssl_context()`
below pins by *exact certificate identity* (loading this one pod's cert as
the sole trusted root via `cadata=`) and disables hostname/IP-SAN checking
(`check_hostname = False`) -- the same trust model SSH's known_hosts pinning
uses, except here there is no "first use" ambiguity at all, since the
manager generated this exact cert itself moments earlier. A cert whose
public key doesn't match the pinned one is rejected exactly as it would be
under IP-SAN validation; a cert presented over a connection to the wrong
IP is not distinguishable from this pinning scheme alone, but is still
only reachable by whichever pod actually holds the matching private key
(itself only ever written to that pod's own Secret-backed volume mount).
"""

from __future__ import annotations

import datetime
import os
import ssl

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from kubernetes_asyncio import client

# Keys within the per-pod sidecar-auth Secret's `string_data` these live
# under (the SAME Secret object SIDECAR_AUTH_SECRET_KEY already lives in --
# see sidecar_auth.py -- not a new Secret; same lifecycle, same deletion
# path via _delete_sidecar_auth_secret()).
SIDECAR_TLS_CERT_SECRET_KEY = "tls_cert"
SIDECAR_TLS_KEY_SECRET_KEY = "tls_key"

# When "true", the sidecar serves plain HTTP and the manager connects over
# plain HTTP -- no cert is generated, no Secret keys are stored, no volume
# is mounted. Defaults to false/unset: TLS is on by default. Intended for
# operators who already run a service mesh (Istio/Linkerd) providing mTLS
# of its own and would rather not pay for boxkite's own cert generation on
# top of it (Option A in the design doc) -- NOT a general escape hatch, and
# NOT recommended for the default self-hosted case this project targets.
SIDECAR_TLS_DISABLED_ENV = "SIDECAR_TLS_DISABLED"

# Mount path the sidecar container reads its cert/key files from (a Secret
# volume, not secretKeyRef -- uvicorn's ssl_certfile/ssl_keyfile need
# filesystem paths, not env vars). See deploy/pod-template.yaml and
# manager.py's/warm_pool.py's sidecar_volume_mounts.
SIDECAR_TLS_MOUNT_PATH = "/etc/boxkite/tls"
SIDECAR_TLS_CERT_FILENAME = "tls.crt"
SIDECAR_TLS_KEY_FILENAME = "tls.key"

# Short validity window: a pod's own lifetime is normally minutes to hours
# (SANDBOX_ACTIVE_DEADLINE_SECONDS defaults to 24h); 7 days is already
# generous slack on top of that and limits exposure if this Secret is ever
# exfiltrated well after its pod is gone.
CERT_VALIDITY_DAYS = 7
# Small backdated `not_valid_before` to absorb clock skew between the
# manager process (which generates the cert) and the sidecar process (which
# serves it) -- both typically run in the same cluster, but a cert valid
# starting at the exact generation instant can fail validation on a sidecar
# whose clock is a few seconds behind.
_NOT_VALID_BEFORE_SKEW_MINUTES = 5


def generate_pod_self_signed_cert(pod_name: str) -> tuple[str, str]:
    """Generate a fresh, self-signed TLS keypair for one pod's sidecar.

    Returns (cert_pem, key_pem) as `str` (PEM-encoded, ready for
    `string_data` in a K8s Secret). A P-256 EC key (equivalent security to
    2048-bit+ RSA at a fraction of the cost -- this is a fresh cert
    generated per pod, on every pod creation, so keeping generation cheap
    matters more than it would for a long-lived cert).

    The Common Name and DNS SAN are both `pod_name` -- not the pod's IP,
    which is unknown at this point (see module docstring's "DEVIATION"
    section for why, and `build_pinned_ssl_context()` for how the manager
    verifies this cert without IP/hostname matching).
    """
    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, pod_name)])

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=_NOT_VALID_BEFORE_SKEW_MINUTES))
        .not_valid_after(now + datetime.timedelta(days=CERT_VALIDITY_DAYS))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(pod_name)]),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=0),
            critical=True,
        )
        .sign(private_key, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return cert_pem, key_pem


def build_pinned_ssl_context(cert_pem: str) -> ssl.SSLContext:
    """Build an `ssl.SSLContext` that trusts exactly one pod's cert.

    Loads `cert_pem` as the sole trusted root (`cadata=`, in-memory --
    deliberately no temp file on disk: this project prefers avoiding
    unnecessary on-disk state, and a leaked temp file per pod over a
    long-running manager process's lifetime is a real cleanup-on-error-path
    risk the design doc calls out explicitly). Hostname/IP-SAN checking is
    disabled (`check_hostname = False`) -- see this module's docstring for
    why: the cert has no IP SAN (the pod's IP isn't known when the cert is
    generated), so pinning happens by exact certificate identity instead of
    by hostname match, the same trust model as SSH's pinned-host-key mode.
    `verify_mode` stays `CERT_REQUIRED`, so a connection presenting any
    cert other than this exact one is still rejected.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata=cert_pem)
    return context


def sidecar_tls_disabled() -> bool:
    """Read the `SIDECAR_TLS_DISABLED` env var (see its constant's docstring).

    A plain function (not a module-level constant) so tests can monkeypatch
    the env var without needing to reimport this module -- matches how
    other per-call env reads in this codebase (e.g.
    `resource_config.build_sidecar_exec_network_isolation_env`) are done.
    """
    return os.environ.get(SIDECAR_TLS_DISABLED_ENV, "").strip().lower() == "true"


def build_sidecar_tls_env(tls_enabled: bool):
    """Build the `SIDECAR_TLS_DISABLED` env var for the sidecar container.

    Mirrors `resource_config.build_sidecar_exec_network_isolation_env`'s
    pattern: the manager's own env (SIDECAR_TLS_DISABLED, read via
    `sidecar_tls_disabled()`) decides whether a cert/key were generated for
    this pod at all (`tls_enabled`), and that decision is forwarded into
    the sidecar container's own env explicitly -- the sidecar and the
    manager are separate processes (often separate deployments entirely)
    that don't share an environment automatically, so this is the only way
    the sidecar learns the manager's decision. The sidecar also
    independently checks whether the cert/key files actually exist at
    `SIDECAR_TLS_MOUNT_PATH` before serving HTTPS (see sidecar/main.py) --
    this env var is belt-and-suspenders, not the sole signal, so an
    operator forcing SIDECAR_TLS_DISABLED=true on a sidecar started outside
    this manager's pod-creation path (e.g. docker-compose) still works.
    """
    return client.V1EnvVar(
        name=SIDECAR_TLS_DISABLED_ENV,
        value="" if tls_enabled else "true",
    )
