"""Tests for the @with_retry decorator."""

from unittest.mock import patch, MagicMock

import pytest

from src.common.retry import with_retry


class TestSuccessOnFirstAttempt:
    @patch("src.common.retry.time.sleep")
    def test_no_sleep_when_succeeds_immediately(self, mock_sleep):
        @with_retry(max_attempts=3, base_delay=1.0, exceptions=[ValueError])
        def succeeds():
            return "ok"

        assert succeeds() == "ok"
        mock_sleep.assert_not_called()


class TestRetryAndRecover:
    @patch("src.common.retry.time.sleep")
    def test_retries_on_configured_exception_and_succeeds(self, mock_sleep):
        call_count = 0

        @with_retry(max_attempts=3, base_delay=1.0, exceptions=[ValueError])
        def fails_once():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("transient")
            return "recovered"

        assert fails_once() == "recovered"
        assert call_count == 2
        mock_sleep.assert_called_once_with(1.0)


class TestExponentialBackoff:
    @patch("src.common.retry.time.sleep")
    def test_delays_are_exponential(self, mock_sleep):
        @with_retry(max_attempts=4, base_delay=1.0, exceptions=[RuntimeError])
        def always_fails():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            always_fails()

        delays = [c.args[0] for c in mock_sleep.call_args_list]
        assert delays == [1.0, 2.0, 4.0]


class TestFixedDelays:
    @patch("src.common.retry.time.sleep")
    def test_uses_fixed_delays(self, mock_sleep):
        @with_retry(
            max_attempts=4,
            exceptions=[RuntimeError],
            fixed_delays=[2, 4, 8],
        )
        def always_fails():
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            always_fails()

        delays = [c.args[0] for c in mock_sleep.call_args_list]
        assert delays == [2, 4, 8]


class TestExhausted:
    @patch("src.common.retry.time.sleep")
    def test_raises_after_max_attempts(self, mock_sleep):
        call_count = 0

        @with_retry(max_attempts=3, base_delay=1.0, exceptions=[ValueError])
        def always_fails():
            nonlocal call_count
            call_count += 1
            raise ValueError(f"attempt {call_count}")

        with pytest.raises(ValueError, match="attempt 3"):
            always_fails()

        assert call_count == 3


class TestUnconfiguredException:
    @patch("src.common.retry.time.sleep")
    def test_does_not_retry_unconfigured_exception(self, mock_sleep):
        @with_retry(max_attempts=3, base_delay=1.0, exceptions=[ValueError])
        def raises_type_error():
            raise TypeError("wrong type")

        with pytest.raises(TypeError):
            raises_type_error()

        mock_sleep.assert_not_called()


class TestOnRetryCallback:
    @patch("src.common.retry.time.sleep")
    def test_callback_invoked_with_attempt_exc_delay(self, mock_sleep):
        callback = MagicMock()

        @with_retry(
            max_attempts=3,
            base_delay=1.0,
            exceptions=[ValueError],
            on_retry=callback,
        )
        def always_fails():
            raise ValueError("boom")

        with pytest.raises(ValueError):
            always_fails()

        assert callback.call_count == 2
        # First retry: attempt=0, delay=1.0
        args0 = callback.call_args_list[0].args
        assert args0[0] == 0
        assert isinstance(args0[1], ValueError)
        assert args0[2] == 1.0
        # Second retry: attempt=1, delay=2.0
        args1 = callback.call_args_list[1].args
        assert args1[0] == 1
        assert args1[2] == 2.0


class TestFunctools:
    def test_wraps_preserves_name_and_docstring(self):
        @with_retry(max_attempts=2, exceptions=[ValueError])
        def my_func():
            """My docstring."""
            return True

        assert my_func.__name__ == "my_func"
        assert my_func.__doc__ == "My docstring."
