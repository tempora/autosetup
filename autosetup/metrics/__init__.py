"""Per-lap metrics computed from a recording.

Bump METRICS_VERSION whenever a metric's definition changes; the store recomputes
recordings that were summarised with an older version.
"""

from autosetup.metrics.compute import METRICS_VERSION, LapResult, compute_recording

__all__ = ["METRICS_VERSION", "LapResult", "compute_recording"]
