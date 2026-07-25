"""Unit tests for src/boxkite/tls.py's per-pod self-signed cert generation.

Isolated from manager.py/warm_pool.py wiring (phase 1 of the design doc's
suggested phasing) -- these tests only exercise cert generation and the
pinned SSLContext builder, with no K8s API, no HTTP, no sidecar process.
"""

import datetime
import ssl

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from boxkite.tls import (
    CERT_VALIDITY_DAYS,
    SIDECAR_TLS_CERT_SECRET_KEY,
    SIDECAR_TLS_DISABLED_ENV,
    SIDECAR_TLS_KEY_SECRET_KEY,
    build_pinned_ssl_context,
    generate_pod_self_signed_cert,
    sidecar_tls_disabled,
)


def test_generate_pod_self_signed_cert_returns_parseable_pem():
    cert_pem, key_pem = generate_pod_self_signed_cert("sandbox-abc123")

    cert = x509.load_pem_x509_certificate(cert_pem.encode("ascii"))
    key = serialization.load_pem_private_key(key_pem.encode("ascii"), password=None)

    assert isinstance(cert, x509.Certificate)
    assert isinstance(key, ec.EllipticCurvePrivateKey)


def test_generate_pod_self_signed_cert_uses_ec_p256_key():
    _, key_pem = generate_pod_self_signed_cert("sandbox-abc123")
    key = serialization.load_pem_private_key(key_pem.encode("ascii"), password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    assert key.curve.name == "secp256r1"
    assert key.key_size >= 256


def test_generate_pod_self_signed_cert_common_name_is_pod_name():
    cert_pem, _ = generate_pod_self_signed_cert("sandbox-xyz789")
    cert = x509.load_pem_x509_certificate(cert_pem.encode("ascii"))
    common_names = cert.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)
    assert common_names[0].value == "sandbox-xyz789"


def test_generate_pod_self_signed_cert_san_is_pod_name():
    cert_pem, _ = generate_pod_self_signed_cert("sandbox-xyz789")
    cert = x509.load_pem_x509_certificate(cert_pem.encode("ascii"))
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == ["sandbox-xyz789"]


def test_generate_pod_self_signed_cert_is_self_signed():
    cert_pem, key_pem = generate_pod_self_signed_cert("sandbox-abc123")
    cert = x509.load_pem_x509_certificate(cert_pem.encode("ascii"))
    key = serialization.load_pem_private_key(key_pem.encode("ascii"), password=None)
    assert cert.issuer == cert.subject
    # Self-signed: the cert's public key must match the generated key.
    assert cert.public_key().public_numbers() == key.public_key().public_numbers()


def test_generate_pod_self_signed_cert_validity_window_is_short_lived():
    cert_pem, _ = generate_pod_self_signed_cert("sandbox-abc123")
    cert = x509.load_pem_x509_certificate(cert_pem.encode("ascii"))

    now = datetime.datetime.now(datetime.timezone.utc)
    lifetime = cert.not_valid_after_utc - cert.not_valid_before_utc

    assert cert.not_valid_before_utc <= now
    assert cert.not_valid_after_utc > now
    # Validity window should be close to CERT_VALIDITY_DAYS (allowing for
    # the small backdated not_valid_before skew), never wildly longer.
    assert lifetime <= datetime.timedelta(days=CERT_VALIDITY_DAYS, hours=1)
    assert lifetime >= datetime.timedelta(days=CERT_VALIDITY_DAYS - 1)


def test_generate_pod_self_signed_cert_is_unique_per_call():
    cert_pem_1, key_pem_1 = generate_pod_self_signed_cert("sandbox-same-name")
    cert_pem_2, key_pem_2 = generate_pod_self_signed_cert("sandbox-same-name")
    assert cert_pem_1 != cert_pem_2
    assert key_pem_1 != key_pem_2


def test_build_pinned_ssl_context_trusts_the_exact_pinned_cert():
    cert_pem, _ = generate_pod_self_signed_cert("sandbox-abc123")
    context = build_pinned_ssl_context(cert_pem)

    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is False


def test_build_pinned_ssl_context_rejects_malformed_pem():
    with pytest.raises(ssl.SSLError):
        build_pinned_ssl_context("not a real certificate")


def test_sidecar_tls_disabled_defaults_to_false(monkeypatch):
    monkeypatch.delenv(SIDECAR_TLS_DISABLED_ENV, raising=False)
    assert sidecar_tls_disabled() is False


@pytest.mark.parametrize("value", ["true", "True", "TRUE", " true "])
def test_sidecar_tls_disabled_true_values(monkeypatch, value):
    monkeypatch.setenv(SIDECAR_TLS_DISABLED_ENV, value)
    assert sidecar_tls_disabled() is True


@pytest.mark.parametrize("value", ["false", "0", "no", ""])
def test_sidecar_tls_disabled_false_values(monkeypatch, value):
    monkeypatch.setenv(SIDECAR_TLS_DISABLED_ENV, value)
    assert sidecar_tls_disabled() is False


def test_secret_key_constants_are_distinct_from_the_auth_token_key():
    from boxkite.sidecar_auth import SIDECAR_AUTH_SECRET_KEY

    assert SIDECAR_TLS_CERT_SECRET_KEY != SIDECAR_AUTH_SECRET_KEY
    assert SIDECAR_TLS_KEY_SECRET_KEY != SIDECAR_AUTH_SECRET_KEY
    assert SIDECAR_TLS_CERT_SECRET_KEY != SIDECAR_TLS_KEY_SECRET_KEY
