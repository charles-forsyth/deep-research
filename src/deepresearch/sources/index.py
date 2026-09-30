"""Saved Gemini File Search indexes for data sources.

A source can have one index: a File Search Store named `deep-research-source-<name>`.
It is built once from the source's readable files and reused by every research run
that includes the source, instead of re-uploading the files each time. The source
records the store name and the manifest hash it was built from
(options.store, options.store_hash); when the source's content changes (new manifest
hash) the index is stale and is rebuilt on the next use or by `sources index`.

`cleanup` never deletes these stores (they are named, and the registry points at them);
`sources index --drop` or `sources rm` removes one.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from deepresearch.sources.adapters import SourceError
from deepresearch.sources.model import DataSource
from deepresearch.sources.registry import SourceRegistry
from deepresearch.sources.usage import research_uploads
from deepresearch.storage.files import SOURCE_STORE_PREFIX


def store_display_name(s: DataSource, workspace: str | None = None) -> str:
    """deep-research-source-<name>, or ...-<workspace>-<name> outside Main. The store is
    found by its id (options.store), never by this name, so two workspaces' sources
    with the same name never share or delete each other's index; the name only helps
    tell them apart in `cleanup` listings."""
    from deepresearch.core import workspace as W

    ws = workspace if workspace is not None else W.current_slug()
    mid = f"{ws}-" if ws and ws != W.MAIN else ""
    return f"{SOURCE_STORE_PREFIX}{mid}{s.name}"


def _ws_of(db_path: str) -> str | None:
    """Workspace id of a registry DB (…/workspaces/<id>/history.db), else None."""
    p = Path(db_path)
    if p.parent.parent.name == "workspaces":
        return p.parent.name
    return None


def index_state(s: DataSource) -> str:
    """'none', 'current' or 'stale'."""
    store = s.options.get("store")
    if not store:
        return "none"
    h = s.manifest.hash if s.manifest else ""
    return "current" if h and s.options.get("store_hash") == h else "stale"


def _delete_store(client: Any, name: str) -> None:
    try:
        for doc in client.file_search_stores.documents.list(parent=name):
            try:
                client.file_search_stores.documents.delete(
                    name=doc.name, config={"force": True}
                )
            except Exception:
                pass
    except Exception:
        pass
    client.file_search_stores.delete(name=name, config={"force": True})


def build_index(
    reg: SourceRegistry, s: DataSource, client: Any, log=print
) -> DataSource:
    """(Re)build the source's index. Returns the updated source."""
    from deepresearch.storage.files import FileManager

    old = s.options.get("store")
    with tempfile.TemporaryDirectory(prefix="dr-index-") as tmp:
        paths, notes = research_uploads([s], Path(tmp))
        for n in notes:
            log(f"[INFO] {n}")
        if not paths:
            raise SourceError(f"{s.name} has no readable files to index")
        fm = FileManager(client)
        store = client.file_search_stores.create(
            config={"display_name": store_display_name(s, _ws_of(reg.db_path))}
        )
        try:
            for folder in paths:
                for f in sorted(Path(folder).iterdir()):
                    if f.is_file():
                        fm._upload_file(str(f), store.name)
        except Exception:
            _delete_store(client, store.name)
            raise
    s = reg.set_options(
        s, store=store.name, store_hash=s.manifest.hash if s.manifest else ""
    )
    if old and old != store.name:
        try:
            _delete_store(client, old)
            log(f"[INFO] removed the old index {old}")
        except Exception as e:
            log(f"[WARN] could not remove the old index {old}: {e}")
    log(f"[INFO] {s.name}: index {store.name}")
    return s


def drop_index(reg: SourceRegistry, s: DataSource, client: Any) -> DataSource:
    store = s.options.get("store")
    if store:
        _delete_store(client, store)
    return reg.set_options(s, store=None, store_hash=None)


def stores_for(
    reg: SourceRegistry, sources: list[DataSource], client: Any | None, log=print
) -> tuple[list[str], list[DataSource]]:
    """Split sources for a research run: indexed ones give a store name (rebuilt first
    when stale); the rest are returned to be uploaded as files."""
    stores: list[str] = []
    rest: list[DataSource] = []
    for s in sources:
        state = index_state(s)
        if state == "none" or client is None:
            rest.append(s)
            continue
        if state == "stale":
            log(
                f"[INFO] {s.name}: content changed since its index was built; rebuilding"
            )
            s = build_index(reg, s, client, log=log)
        stores.append(str(s.options["store"]))
    return stores, rest
