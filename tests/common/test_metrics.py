"""Tests for CloudWatch metrics publisher."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.common.metrics import (
    METRIC_NAMESPACE,
    publish_metrics,
)


class TestPublishMetrics:
    def test_publishes_single_metric(self):
        cw = MagicMock()

        publish_metrics(cw, [
            {"MetricName": "test-metric", "Value": 42, "Unit": "Count"},
        ])

        cw.put_metric_data.assert_called_once()
        call_kwargs = cw.put_metric_data.call_args[1]
        assert call_kwargs["Namespace"] == METRIC_NAMESPACE

        metric_data = call_kwargs["MetricData"]
        assert len(metric_data) == 1
        assert metric_data[0]["MetricName"] == "test-metric"
        assert metric_data[0]["Value"] == 42
        assert metric_data[0]["Unit"] == "Count"

    def test_includes_default_dimensions(self):
        cw = MagicMock()

        publish_metrics(cw, [
            {"MetricName": "m", "Value": 1, "Unit": "Count"},
        ])

        dims = cw.put_metric_data.call_args[1]["MetricData"][0]["Dimensions"]
        dim_names = {d["Name"] for d in dims}
        assert "Environment" in dim_names
        assert "Project" in dim_names

    def test_merges_custom_dimensions(self):
        cw = MagicMock()

        publish_metrics(cw, [
            {
                "MetricName": "m",
                "Value": 1,
                "Unit": "Count",
                "Dimensions": [{"Name": "Custom", "Value": "val"}],
            },
        ])

        dims = cw.put_metric_data.call_args[1]["MetricData"][0]["Dimensions"]
        dim_names = {d["Name"] for d in dims}
        assert "Environment" in dim_names
        assert "Project" in dim_names
        assert "Custom" in dim_names

    def test_publishes_multiple_metrics(self):
        cw = MagicMock()

        publish_metrics(cw, [
            {"MetricName": "m1", "Value": 10, "Unit": "Count"},
            {"MetricName": "m2", "Value": 20, "Unit": "None"},
        ])

        metric_data = cw.put_metric_data.call_args[1]["MetricData"]
        assert len(metric_data) == 2
        assert metric_data[0]["MetricName"] == "m1"
        assert metric_data[1]["MetricName"] == "m2"

    def test_defaults_unit_to_none(self):
        cw = MagicMock()

        publish_metrics(cw, [
            {"MetricName": "m", "Value": 1.5},
        ])

        metric_data = cw.put_metric_data.call_args[1]["MetricData"]
        assert metric_data[0]["Unit"] == "None"

    def test_noop_when_client_is_none(self):
        publish_metrics(None, [
            {"MetricName": "m", "Value": 1, "Unit": "Count"},
        ])

    def test_noop_when_metrics_empty(self):
        cw = MagicMock()
        publish_metrics(cw, [])
        cw.put_metric_data.assert_not_called()

    def test_batches_in_chunks_of_20(self):
        cw = MagicMock()

        metrics = [
            {"MetricName": f"m{i}", "Value": i, "Unit": "Count"}
            for i in range(25)
        ]

        publish_metrics(cw, metrics)

        assert cw.put_metric_data.call_count == 2
        first_batch = cw.put_metric_data.call_args_list[0][1]["MetricData"]
        second_batch = cw.put_metric_data.call_args_list[1][1]["MetricData"]
        assert len(first_batch) == 20
        assert len(second_batch) == 5

    def test_swallows_exceptions(self):
        cw = MagicMock()
        cw.put_metric_data.side_effect = RuntimeError("CloudWatch down")

        publish_metrics(cw, [
            {"MetricName": "m", "Value": 1, "Unit": "Count"},
        ])

    def test_swallows_malformed_metric(self):
        cw = MagicMock()

        # Missing MetricName key — should not raise
        publish_metrics(cw, [{"Value": 1}])


class TestEnvironmentDimension:
    """ENVIRONMENT wins; STAGE is the fallback set by deploy scripts."""

    def _env_value(self, cw):
        dims = cw.put_metric_data.call_args[1]["MetricData"][0]["Dimensions"]
        return next(d["Value"] for d in dims if d["Name"] == "Environment")

    def test_environment_var_wins(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "explicit")
        monkeypatch.setenv("STAGE", "dev")
        cw = MagicMock()

        publish_metrics(cw, [{"MetricName": "m", "Value": 1, "Unit": "Count"}])

        assert self._env_value(cw) == "explicit"

    def test_stage_fallback_when_environment_unset(self, monkeypatch):
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.setenv("STAGE", "staging")
        cw = MagicMock()

        publish_metrics(cw, [{"MetricName": "m", "Value": 1, "Unit": "Count"}])

        assert self._env_value(cw) == "staging"

    def test_defaults_to_dev_when_neither_set(self, monkeypatch):
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.delenv("STAGE", raising=False)
        cw = MagicMock()

        publish_metrics(cw, [{"MetricName": "m", "Value": 1, "Unit": "Count"}])

        assert self._env_value(cw) == "dev"

    def test_resolved_per_call_not_at_import(self, monkeypatch):
        # Simulate STAGE being set after metrics module was imported —
        # the bug we're fixing: original DEFAULT_DIMENSIONS captured value
        # at import time and ignored later env changes.
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        monkeypatch.setenv("STAGE", "staging")

        cw = MagicMock()
        publish_metrics(cw, [{"MetricName": "m", "Value": 1, "Unit": "Count"}])
        assert self._env_value(cw) == "staging"

        cw.reset_mock()
        monkeypatch.setenv("STAGE", "dev")
        publish_metrics(cw, [{"MetricName": "m", "Value": 1, "Unit": "Count"}])
        assert self._env_value(cw) == "dev"
