"""agent-wingman: code index and knowledge store for AI agents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("agent-wingman")
except PackageNotFoundError:  # running from a source checkout without install
    __version__ = "0.0.0+unknown"
