"""Retry-layer tests for both clients. httpx.MockTransport drives the
response sequence; time.sleep / asyncio.sleep are monkeypatched so the
suite never actually waits on a backoff."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from boxkite_client import AsyncBoxkiteClient, BoxkiteApiError, BoxkiteClient, RetryConfig
from boxkite_client.client import _parse_retry_after, _retry_delay, _should_retry

FAST_RETRY = RetryConfig(max_retries=2, backoff_base=0.0)


def _sequence_handler(responses: list[httpx.Response]) -> tuple[list[httpx.Request], object]:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    return calls, handler


def _client(handler, retry: RetryConfig | None = FAST_RETRY) -> BoxkiteClient:
    return BoxkiteClient(
        base_url="https://cp.example.com",
        api_key="bxk_live_test",
        retry=retry,
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("boxkite_client.client.time.sleep", lambda s: slept.append(s))

    async def _fake_asleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr("boxkite_client.client.asyncio.sleep", _fake_asleep)
    return slept


def test_retries_5xx_then_succeeds():
    calls, handler = _sequence_handler(
        [httpx.Response(503, json={"error": {"code": "unavailable", "message": "nope"}}),
         httpx.Response(200, json={"id": "acct-1"})]
    )
    assert _client(handler).account() == {"id": "acct-1"}
    assert len(calls) == 2


def test_retries_429_then_succeeds():
    calls, handler = _sequence_handler(
        [httpx.Response(429, json={"error": {"code": "rate_limited", "message": "slow down"}}),
         httpx.Response(200, json={"id": "acct-1"})]
    )
    assert _client(handler).account() == {"id": "acct-1"}
    assert len(calls) == 2


def test_gives_up_after_max_retries_and_raises():
    calls, handler = _sequence_handler(
        [httpx.Response(503, json={"error": {"code": "unavailable", "message": "down"}})]
    )
    with pytest.raises(BoxkiteApiError) as exc:
        _client(handler).account()
    assert exc.value.status_code == 503
    assert len(calls) == 3  # initial + 2 retries


def test_does_not_retry_when_disabled():
    calls, handler = _sequence_handler(
        [httpx.Response(503, json={"error": {"code": "unavailable", "message": "down"}})]
    )
    with pytest.raises(BoxkiteApiError):
        _client(handler, retry=None).account()
    assert len(calls) == 1


def test_does_not_retry_non_idempotent_post():
    calls, handler = _sequence_handler(
        [httpx.Response(503, json={"error": {"code": "unavailable", "message": "down"}})]
    )
    with pytest.raises(BoxkiteApiError):
        _client(handler).create_sandbox(label="x")
    assert len(calls) == 1  # POST is never blind-retried


def test_does_not_retry_4xx_other_than_429():
    calls, handler = _sequence_handler(
        [httpx.Response(404, json={"error": {"code": "not_found", "message": "gone"}})]
    )
    with pytest.raises(BoxkiteApiError):
        _client(handler).get_sandbox("s1")
    assert len(calls) == 1


def test_retries_connection_error():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"id": "acct-1"})

    assert _client(handler).account() == {"id": "acct-1"}
    assert len(calls) == 2


def test_honors_retry_after_header(_no_sleep):
    calls, handler = _sequence_handler(
        [httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"code": "rl", "message": "x"}}),
         httpx.Response(200, json={"id": "acct-1"})]
    )
    # backoff_base high enough that computed backoff would differ from 7 --
    # proves the header value was used, not exponential backoff.
    retry = RetryConfig(max_retries=2, backoff_base=100.0, backoff_max=1000.0)
    assert _client(handler, retry=retry).account() == {"id": "acct-1"}
    assert _no_sleep == [7.0]


def test_async_retries_5xx_then_succeeds():
    calls, handler = _sequence_handler(
        [httpx.Response(502, json={"error": {"code": "bad_gateway", "message": "x"}}),
         httpx.Response(200, json={"id": "acct-1"})]
    )

    async def run() -> dict:
        client = AsyncBoxkiteClient(
            base_url="https://cp.example.com",
            api_key="bxk_live_test",
            retry=FAST_RETRY,
            transport=httpx.MockTransport(handler),
        )
        try:
            return await client.account()
        finally:
            await client.aclose()

    assert asyncio.run(run()) == {"id": "acct-1"}
    assert len(calls) == 2


def test_parse_retry_after_seconds():
    assert _parse_retry_after("5") == 5.0
    assert _parse_retry_after("  0 ") == 0.0
    assert _parse_retry_after("not-a-date") is None
    assert _parse_retry_after("") is None


def test_parse_retry_after_http_date_in_past_clamps_to_zero():
    assert _parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


def test_retry_delay_prefers_retry_after():
    cfg = RetryConfig(backoff_base=100.0, backoff_max=1000.0)
    assert _retry_delay(cfg, 3, 4.0) == 4.0


def test_retry_delay_caps_at_backoff_max():
    cfg = RetryConfig(backoff_base=1.0, backoff_max=2.0)
    assert 0.0 <= _retry_delay(cfg, 10, None) <= 2.0


def test_should_retry_respects_method_and_attempt():
    cfg = RetryConfig(max_retries=2)
    assert _should_retry(cfg, "GET", 0, 503) is True
    assert _should_retry(cfg, "POST", 0, 503) is False
    assert _should_retry(cfg, "GET", 2, 503) is False
    assert _should_retry(None, "GET", 0, 503) is False
