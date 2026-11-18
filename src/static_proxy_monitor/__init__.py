"""Check static proxies: exit IP identity, location agreement, target sites, changes between runs."""

from ._version import __version__
from .monitor import Entry, GeoView, Result, TargetCheck, changes, check_all, check_entry, parse_entries

__all__ = [
    "Entry",
    "GeoView",
    "Result",
    "TargetCheck",
    "__version__",
    "changes",
    "check_all",
    "check_entry",
    "parse_entries",
]
