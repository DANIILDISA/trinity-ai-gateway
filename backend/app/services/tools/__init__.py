"""Agentic tool-use: registry, file-tool executors, and the line-diff helper.

Importing this package registers the built-in tools as a side effect (file
tools), so the engine sees them via `registry.definitions()`.
"""
from app.services.tools import file_tools as _file_tools  # noqa: F401  (registers tools)
