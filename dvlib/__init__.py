"""DVLib public package metadata."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("dvlib")
except PackageNotFoundError:  # Source tree used without an installed distribution.
    __version__ = "0.1.0"

__all__ = ["__version__"]
