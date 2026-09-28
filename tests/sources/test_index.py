"""Saved File Search indexes for data sources (fake Gemini client)."""

from types import SimpleNamespace

import pytest

from deepresearch.sources import DataSource, SourceRegistry
from deepresearch.sources import index as ix
from deepresearch.sources.service import check
from deepresearch.storage.files import is_disposable_store


class FakeStores:
    def __init__(self):
        self.stores: dict[str, list[str]] = {}
        self.display: dict[str, str] = {}
        self.n = 0
        self.documents = SimpleNamespace(
            list=lambda parent: [
                SimpleNamespace(name=f"{parent}/d{i}")
                for i, _ in enumerate(self.stores.get(parent, []))
            ],
            delete=lambda name, config=None: None,
        )

    def create(self, config=None):
        self.n += 1
        name = f"fileSearchStores/s{self.n}"
        self.stores[name] = []
        self.display[name] = (config or {}).get("display_name", "")
        return SimpleNamespace(name=name, display_name=self.display[name])

    def upload_to_file_search_store(self, file_search_store_name, file, config=None):
        self.stores[file_search_store_name].append(file)

    def delete(self, name, config=None):
        self.stores.pop(name, None)


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "d").mkdir(parents=True)
    (home / "d" / "a.md").write_text("alpha")
    (home / "d" / "b.csv").write_text("x,y\n")
    monkeypatch.setenv("DR_LOCAL_ROOTS", str(home))
    reg = SourceRegistry(str(tmp_path / "h.db"))
    s = check(
        reg, reg.add(DataSource(name="docs", kind="local_folder", uri=str(home / "d")))
    )
    client = SimpleNamespace(file_search_stores=FakeStores())
    return reg, s, client, home


def test_build_index_uploads_and_records(env):
    reg, s, client, _ = env
    assert ix.index_state(s) == "none"
    s = ix.build_index(reg, s, client, log=lambda m: None)
    fs = client.file_search_stores
    store = s.options["store"]
    assert sorted(p.rsplit("/", 1)[1] for p in fs.stores[store]) == ["a.md", "b.csv"]
    assert fs.display[store] == "deep-research-source-docs"
    assert ix.index_state(reg.get("docs")) == "current"
    # cleanup must never treat it as disposable
    assert not is_disposable_store(
        SimpleNamespace(name=store, display_name=fs.display[store])
    )


def test_stale_index_is_rebuilt_and_old_store_removed(env):
    reg, s, client, home = env
    s = ix.build_index(reg, s, client, log=lambda m: None)
    old = s.options["store"]
    (home / "d" / "c.txt").write_text("new file")
    s = check(reg, reg.get("docs"))  # new manifest hash
    assert ix.index_state(s) == "stale"
    stores, rest = ix.stores_for(reg, [s], client, log=lambda m: None)
    assert rest == [] and stores[0] != old
    assert old not in client.file_search_stores.stores  # old index deleted
    assert ix.index_state(reg.get("docs")) == "current"


def test_unindexed_sources_are_uploaded_and_drop_index(env):
    reg, s, client, _ = env
    stores, rest = ix.stores_for(reg, [s], client)
    assert stores == [] and [r.name for r in rest] == ["docs"]
    s = ix.build_index(reg, s, client, log=lambda m: None)
    store = s.options["store"]
    s = ix.drop_index(reg, s, client)
    assert "store" not in s.options and store not in client.file_search_stores.stores


def test_cli_research_uses_index_as_store(env, monkeypatch):
    from unittest.mock import MagicMock

    from deepresearch.cli import commands

    reg, s, client, _ = env
    ix.build_index(reg, s, client, log=lambda m: None)
    monkeypatch.setattr(commands, "user_db_path", reg.db_path)
    monkeypatch.setattr(commands.genai, "Client", lambda **k: client)
    monkeypatch.setattr(
        commands, "DeepResearchConfig", lambda: SimpleNamespace(api_key="k")
    )
    seen = {}

    class Agent:
        def __init__(self, **k):
            pass

        def start_research_poll(self, request):
            seen["stores"], seen["uploads"] = request.stores, request.upload_paths

    monkeypatch.setattr(commands, "DeepResearchAgent", Agent)
    args = MagicMock(
        prompt="p", stores=None, stream=False, format=None, upload=None, output=None,
        adopt_session=None, depth=1, breadth=3, quiet=True, source=["docs"],
    )  # fmt: skip
    commands.handle_research(args)
    assert seen["stores"] == [reg.get("docs").options["store"]] and not seen["uploads"]
