import pytest

from mavixboard.core.backoff import ExponentialBackoff


def test_default_sequence():
    b = ExponentialBackoff()
    delays = [b.next_delay() for _ in range(7)]
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_custom_initial_and_multiplier():
    b = ExponentialBackoff(initial=0.5, multiplier=3.0, cap=100.0)
    delays = [b.next_delay() for _ in range(5)]
    assert delays == [0.5, 1.5, 4.5, 13.5, 40.5]


def test_cap_caps_at_value():
    b = ExponentialBackoff(initial=10, multiplier=2, cap=20)
    delays = [b.next_delay() for _ in range(5)]
    assert delays == [10.0, 20.0, 20.0, 20.0, 20.0]


def test_reset_returns_to_initial():
    b = ExponentialBackoff(initial=1, multiplier=2, cap=30)
    for _ in range(5):
        b.next_delay()
    b.reset()
    assert b.next_delay() == 1.0


def test_current_property_reflects_next_value():
    b = ExponentialBackoff()
    assert b.current == 1.0
    b.next_delay()
    assert b.current == 2.0


def test_rejects_invalid_initial():
    with pytest.raises(ValueError):
        ExponentialBackoff(initial=0)
    with pytest.raises(ValueError):
        ExponentialBackoff(initial=-1)


def test_rejects_multiplier_below_one():
    with pytest.raises(ValueError):
        ExponentialBackoff(multiplier=0.5)


def test_rejects_cap_below_initial():
    with pytest.raises(ValueError):
        ExponentialBackoff(initial=10, cap=5)


def test_multiplier_exactly_one_is_allowed():
    b = ExponentialBackoff(initial=2, multiplier=1.0, cap=2.0)
    delays = [b.next_delay() for _ in range(4)]
    assert delays == [2.0, 2.0, 2.0, 2.0]
