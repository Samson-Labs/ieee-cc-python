"""CloudWatch metrics publisher for IEEE-RC AI pipeline.

Publishes custom metrics to the ``ieee-rc`` namespace with standard
dimensions (Environment, Project).  All publish calls are fire-and-forget:
failures are logged as warnings and never interrupt the calling Lambda.
"""

from __future__ import annotations

import logging
import os
from typing import Sequence

logger = logging.getLogger(__name__)

METRIC_NAMESPACE = "ieee-rc"
DEFAULT_DIMENSIONS = [
    {"Name": "Environment", "Value": os.environ.get("ENVIRONMENT", "dev")},
    {"Name": "Project", "Value": "ieee-rc"},
]


def publish_metrics(
    cloudwatch_client,
    metrics: Sequence[dict],
) -> None:
    """Publish one or more metrics to CloudWatch.

    Each entry in *metrics* must have:
        - MetricName (str)
        - Value (int | float)
        - Unit (str) — e.g. "Count", "None"

    Optional per-metric keys:
        - Dimensions (list[dict]) — merged with the defaults

    Args:
        cloudwatch_client: A boto3 CloudWatch client.
        metrics: Sequence of metric dicts.
    """
    if cloudwatch_client is None:
        return

    metric_data = []
    for m in metrics:
        dims = list(DEFAULT_DIMENSIONS)
        if "Dimensions" in m:
            dims = dims + m["Dimensions"]
        metric_data.append({
            "MetricName": m["MetricName"],
            "Value": m["Value"],
            "Unit": m.get("Unit", "None"),
            "Dimensions": dims,
        })

    try:
        cloudwatch_client.put_metric_data(
            Namespace=METRIC_NAMESPACE,
            MetricData=metric_data,
        )
    except Exception:
        logger.warning(
            "Failed to publish CloudWatch metrics: %s",
            [m["MetricName"] for m in metric_data],
            exc_info=True,
        )
