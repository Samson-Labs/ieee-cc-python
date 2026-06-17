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


def _default_dimensions() -> list[dict]:
    # Resolved per-call so a Lambda's STAGE/ENVIRONMENT env vars are
    # respected even if set after module import.  ENVIRONMENT wins for
    # explicit override; STAGE is the deploy-script-set fallback.
    env = os.environ.get("ENVIRONMENT", os.environ.get("STAGE", "dev"))
    return [
        {"Name": "Environment", "Value": env},
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
    if cloudwatch_client is None or not metrics:
        return

    try:
        defaults = _default_dimensions()
        metric_data = []
        for m in metrics:
            dims = list(defaults)
            custom_dims = m.get("Dimensions")
            if isinstance(custom_dims, list):
                dims.extend(custom_dims)
            metric_data.append({
                "MetricName": m["MetricName"],
                "Value": m["Value"],
                "Unit": m.get("Unit", "None"),
                "Dimensions": dims,
            })

        for i in range(0, len(metric_data), 20):
            batch = metric_data[i : i + 20]
            cloudwatch_client.put_metric_data(
                Namespace=METRIC_NAMESPACE,
                MetricData=batch,
            )
    except Exception:
        logger.warning(
            "Failed to publish CloudWatch metrics: %s",
            [m.get("MetricName", "unknown") for m in metrics],
            exc_info=True,
        )
