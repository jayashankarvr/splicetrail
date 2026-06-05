"""GPU acceleration layer with CPU fallback."""

from splicetrail.acceleration.backend import (
    get_backend,
    Backend,
    CUPY_AVAILABLE,
)

__all__ = ["get_backend", "Backend", "CUPY_AVAILABLE"]
