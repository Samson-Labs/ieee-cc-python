"""Shared pytest fixtures for the ieee-cc-python test suite."""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError


@pytest.fixture
def mock_s3_client():
    return MagicMock()


@pytest.fixture
def mock_lambda_client():
    return MagicMock()


@pytest.fixture
def mock_sns_client():
    return MagicMock()


@pytest.fixture
def make_client_error():
    """Factory fixture that returns a function for creating ClientError instances."""

    def _make(code: str = "NoSuchKey", message: str = "Not found") -> ClientError:
        return ClientError(
            {"Error": {"Code": code, "Message": message}},
            "TestOperation",
        )

    return _make
