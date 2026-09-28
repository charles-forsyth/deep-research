"""Data sources: named places data lives, shared by the CLI, the dashboard and Lab runs.

A source is a reference (kind + URI + a credential *reference*), never the data or the
secret itself. Adapters in :mod:`deepresearch.sources.adapters` know how to test, list,
preview and fetch each kind; :class:`SourceRegistry` stores sources in the history DB.
"""

from deepresearch.sources.model import KINDS, LEVELS, DataSource, Manifest
from deepresearch.sources.registry import SourceRegistry

__all__ = ["KINDS", "LEVELS", "DataSource", "Manifest", "SourceRegistry"]
