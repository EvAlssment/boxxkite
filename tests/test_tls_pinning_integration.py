"""End-to-end TLS handshake tests for tls.py's pinning scheme.

tests/test_tls.py covers cert generation and SSLContext construction in
isolation; this file goes one step further and actually performs a real TLS
handshake over a loopback socket using the generated cert/key and the
pinned SSLContext, to catch the two failure modes the design doc calls out
explicitly (docs/SIDECAR-TRANSPORT-TLS-DESIGN.md §6):

- A pinned client must actually SUCCEED against the exact cert it was
  pinned to (not just construct an SSLContext that looks right).
- A pinned client must actually FAIL (loudly) against any other cert,
  including a cert for a completely different pod.
"""

import socket
import ssl
import threading

import pytest

from boxkite.tls import build_pinned_ssl_context, generate_pod_self_signed_cert


def _serve_once_tls(server_sock: socket.socket, cert_path: str, key_path: str) -> None:
    """Accept exactly one TLS connection, echo one line, close."""
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    conn, _addr = server_sock.accept()
    try:
        tls_conn = server_context.wrap_socket(conn, server_side=True)
        try:
            tls_conn.sendall(b"hello\n")
        finally:
            tls_conn.close()
    except Exception:
        conn.close()


@pytest.fixture
def tls_server(tmp_path):
    """Starts a real TLS server on loopback serving a fresh per-pod cert.

    Yields (host, port, cert_pem) and tears the server thread down after
    the test (a single accept() naturally returns once the client
    connects and the thread's target function completes).
    """
    cert_pem, key_pem = generate_pod_self_signed_cert("sandbox-integration-test")
    cert_path = tmp_path / "tls.crt"
    key_path = tmp_path / "tls.key"
    cert_path.write_text(cert_pem)
    key_path.write_text(key_pem)

    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    host, port = server_sock.getsockname()

    thread = threading.Thread(
        target=_serve_once_tls, args=(server_sock, str(cert_path), str(key_path)), daemon=True
    )
    thread.start()

    yield host, port, cert_pem

    thread.join(timeout=5)
    server_sock.close()


def test_pinned_context_completes_handshake_against_the_exact_pinned_cert(tls_server):
    host, port, cert_pem = tls_server
    context = build_pinned_ssl_context(cert_pem)

    with socket.create_connection((host, port), timeout=5) as raw_sock:
        with context.wrap_socket(raw_sock) as tls_sock:
            line = tls_sock.recv(1024)
            assert line == b"hello\n"


def test_pinned_context_rejects_a_different_pods_cert(tls_server):
    """The server presents "sandbox-integration-test"'s cert, but the client
    is pinned to a DIFFERENT pod's cert entirely -- the handshake must fail
    loudly (ssl.SSLError), never silently succeed."""
    host, port, _server_cert_pem = tls_server
    wrong_cert_pem, _wrong_key_pem = generate_pod_self_signed_cert("sandbox-a-totally-different-pod")
    context = build_pinned_ssl_context(wrong_cert_pem)

    with pytest.raises(ssl.SSLError):
        with socket.create_connection((host, port), timeout=5) as raw_sock:
            with context.wrap_socket(raw_sock):
                pass
