# Unit tests for the shared DB retry helper (pure; no node needed).
# Run with: python3 -m pytest -v

import pytest

import db_helpers


def test_success_returns_value():
    assert db_helpers.retry_db(lambda: 42) == 42


def test_abort_returns_none_without_retry():
    calls = {"n": 0}

    def op():
        calls["n"] += 1
        raise ValueError("boom")

    assert db_helpers.retry_db(op, abort_on=(ValueError,), delay=0) is None
    assert calls["n"] == 1  # aborted on first failure, no retry


def test_non_abort_exception_is_retried():
    state = {"n": 0}

    def op():
        state["n"] += 1
        if state["n"] < 3:
            raise RuntimeError("transient")
        return "ok"

    assert db_helpers.retry_db(op, abort_on=(ValueError,), delay=0) == "ok"
    assert state["n"] == 3


def test_max_tries_invokes_on_give_up():
    def op():
        raise RuntimeError("always")

    assert db_helpers.retry_db(op, delay=0, max_tries=5, on_give_up=lambda: "gaveup") == "gaveup"


def test_max_tries_reraises_without_handler():
    def op():
        raise RuntimeError("always")

    with pytest.raises(RuntimeError):
        db_helpers.retry_db(op, delay=0, max_tries=3)
