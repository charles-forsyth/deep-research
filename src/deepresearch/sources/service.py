"""Operations on sources that touch both the registry and the adapters."""

from __future__ import annotations

from datetime import datetime

from deepresearch.sources.adapters import SourceError, adapter_for
from deepresearch.sources.model import DataSource, Manifest
from deepresearch.sources.registry import SourceRegistry


def check(reg: SourceRegistry, s: DataSource, full: bool = True) -> DataSource:
    """Reach the source, store its manifest and status. Never raises SourceError."""
    s.last_checked = datetime.now().isoformat(timespec="seconds")
    try:
        a = adapter_for(s)
        m = a.manifest() if full else a.test()
        s.manifest = Manifest(
            **{k: v for k, v in m.model_dump().items() if k in Manifest.model_fields}
        )
        s.status, s.last_error = "ok", ""
    except SourceError as e:
        s.status, s.last_error = "unreachable", str(e)
    if s.id is not None:
        reg.update(s)
    return s
