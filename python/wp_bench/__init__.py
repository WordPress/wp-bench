"""WP-Bench Python harness package."""
from importlib import metadata

__all__ = ["__version__"]

try:
    __version__ = metadata.version("wp-bench")
except metadata.PackageNotFoundError:  # pragma: no cover
    __version__ = "0.1.0"
