from __future__ import annotations

from boxkite_bastion.rate_limit import PerHostConnectionLimiter


def test_try_acquire_succeeds_up_to_the_configured_max():
    limiter = PerHostConnectionLimiter(max_connections_per_host=2)
    assert limiter.try_acquire("1.2.3.4") is True
    assert limiter.try_acquire("1.2.3.4") is True


def test_try_acquire_fails_once_the_max_is_reached():
    limiter = PerHostConnectionLimiter(max_connections_per_host=2)
    limiter.try_acquire("1.2.3.4")
    limiter.try_acquire("1.2.3.4")
    assert limiter.try_acquire("1.2.3.4") is False


def test_different_hosts_do_not_share_a_budget():
    limiter = PerHostConnectionLimiter(max_connections_per_host=1)
    assert limiter.try_acquire("1.2.3.4") is True
    assert limiter.try_acquire("5.6.7.8") is True


def test_release_frees_a_slot_for_reuse():
    limiter = PerHostConnectionLimiter(max_connections_per_host=1)
    limiter.try_acquire("1.2.3.4")
    assert limiter.try_acquire("1.2.3.4") is False
    limiter.release("1.2.3.4")
    assert limiter.try_acquire("1.2.3.4") is True


def test_release_on_a_host_with_no_tracked_connections_is_a_no_op():
    limiter = PerHostConnectionLimiter(max_connections_per_host=1)
    limiter.release("never-acquired")  # must not raise or go negative
    assert limiter.try_acquire("never-acquired") is True
    assert limiter.try_acquire("never-acquired") is False
