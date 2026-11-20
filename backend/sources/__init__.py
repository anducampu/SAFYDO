"""Source-plugin registry. Add a provider class here to make it available."""

from __future__ import annotations

from .jamendo import Jamendo
from .radiobrowser import RadioBrowser

# id -> provider class. Order defines tab order in the UI.
PROVIDERS: dict[str, type] = {p.id: p for p in (RadioBrowser, Jamendo)}
