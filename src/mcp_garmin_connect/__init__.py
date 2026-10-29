"""Garmin Connect MCP server package."""

__all__ = ["__version__"]

__version__ = "0.1.0"

# Load the tool registry first: tools.py pulls in extended_tools and coach at its end,
# and those import helpers back from tools, so importing them directly would otherwise
# hit a partially initialised module.
from . import tools as _tools  # noqa: E402, F401
