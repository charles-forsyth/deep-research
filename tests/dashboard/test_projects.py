"""Projects (SPEC section 22): store rules, API, AI features with fakes, exports."""

import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from deepresearch.dashboard import projects as pj
from deepresearch.dashboard.projects import ProjectStore
from tests.dashboard.test_server import _seed, app  # noqa: F401  (fixture)


def _child(api, parent_id, prompt="child"):
    sid = api.sessions.create_session(
        f"iid_c{parent_id}_{prompt}", prompt, parent_id=parent_id
    )
    api.sessions.update_session(f"iid_c{parent_id}_{prompt}", "completed", "child body")
    return sid


def _vec(*xs):
    return json.dumps(list(xs))


# ---------------------------------------------------------------- store rules


def test_first_project_is_home_and_second_only_links(app):  # noqa: F811
    api = app["api"]
    sid = _seed(api)
    st: ProjectStore = api.projects
    a = st.create("Grant A")
    b = st.create("Paper B")
    st.add_item(a["id"], "session", sid)
    st.add_item(b["id"], "session", sid)
    homes = {p["id"]: p["is_home"] for p in st.projects_for("session", sid)}
    assert homes == {a["id"]: True, b["id"]: False}
    st.set_home(b["id"], sid)
    homes = {p["id"]: p["is_home"] for p in st.projects_for("session", sid)}
    assert homes == {a["id"]: False, b["id"]: True}
    assert st.home_project(sid)["id"] == b["id"]


def test_removing_home_rehomes_to_remaining_project(app):  # noqa: F811
    api = app["api"]
    sid = _seed(api)
    st = api.projects
    a, b = st.create("A"), st.create("B")
    st.add_item(a["id"], "session", sid)
    st.add_item(b["id"], "session", sid)
    st.remove_item(a["id"], "session", sid)
    assert st.home_project(sid)["id"] == b["id"]
    st.delete(b["id"])
    assert st.home_project(sid) is None
    assert sid in st.inbox_ids()


def test_children_follow_their_root(app):  # noqa: F811
    api = app["api"]
    root = _seed(api)
    kid = _child(api, root)
    grandkid = _child(api, kid, "gk")
    st = api.projects
    p = st.create("P")
    st.add_item(p["id"], "session", grandkid)  # filing a sub-report files its root
    assert st.item_ids(p["id"], "session") == [root]
    assert set(st.session_ids(p["id"])) == {root, kid, grandkid}
    assert [x["id"] for x in st.projects_for("session", grandkid)] == [p["id"]]


def test_inbox_is_unfiled_top_level_only(app):  # noqa: F811
    api = app["api"]
    a = _seed(api, prompt="alpha")
    b = _seed(api, prompt="bravo")
    _child(api, a)
    st = api.projects
    p = st.create("P")
    st.add_item(p["id"], "session", a)
    assert st.inbox_ids() == [b]


def test_title_validation_and_levels(app):  # noqa: F811
    st = app["api"].projects
    st.create("Thesis")
    with pytest.raises(ValueError):
        st.create("thesis")  # case-insensitive duplicate
    with pytest.raises(ValueError):
        st.create("   ")
    with pytest.raises(ValueError):
        st.create("X", protection_level="P9")
    with pytest.raises(ValueError):
        st.create("Y", lab_partition="bad name;rm")
    assert pj.strictest(["P2", "P4", "P1"]) == "P4"
    assert pj.strictest([]) == "P2"


# ---------------------------------------------------------------- API


def test_crud_and_counts(app):  # noqa: F811
    call, api = app["call"], app["api"]
    sid = _seed(api)
    st, p = call(
        "POST", "/api/projects", {"title": "NSF CAREER", "nexus_ref": "GRANT-42"}
    )
    assert st == 200 and p["nexus_ref"] == "GRANT-42"
    st, _ = call(
        "POST", f"/api/projects/{p['id']}/items", {"kind": "session", "ids": [sid]}
    )
    assert st == 200
    st, lst = call("GET", "/api/projects")
    assert lst["projects"][0]["counts"]["session"] == 1 and lst["inbox"] == 0
    st, _ = call(
        "PATCH",
        f"/api/projects/{p['id']}",
        {"color": "amber", "protection_level": "P3"},
    )
    st, got = call("GET", f"/api/projects/{p['id']}")
    assert got["project"]["color"] == "amber" and got["effective_level"] == "P3"
    assert got["reports"][0]["id"] == sid and got["reports"][0]["is_home"]
    st, _ = call("PATCH", f"/api/projects/{p['id']}", {"color": "chartreuse"})
    assert st == 400
    st, _ = call("DELETE", f"/api/projects/{p['id']}")
    assert st == 200
    st, _ = call("GET", f"/api/projects/{p['id']}")
    assert st == 404
    st, s = call("GET", f"/api/sessions/{sid}")
    assert s["projects"] == []  # the report itself survives


def test_session_list_and_detail_carry_projects_and_defaults(app):  # noqa: F811
    call, api = app["call"], app["api"]
    sid = _seed(api)
    p = api.projects.create("P", lab_partition="computehigh", protection_level="P3")
    api.projects.add_item(p["id"], "session", sid)
    st, lst = call("GET", "/api/sessions")
    row = next(r for r in lst["sessions"] if r["id"] == sid)
    assert row["projects"][0]["title"] == "P"
    st, s = call("GET", f"/api/sessions/{sid}")
    d = s["project_defaults"]
    assert d["project_id"] == p["id"] and d["lab_partition"] == "computehigh"
    assert d["protection_level"] == "P3"


def test_research_launch_files_into_project(app):  # noqa: F811
    call, api = app["call"], app["api"]
    p = api.projects.create("Launch target")
    st, r = call(
        "POST", "/api/research", {"prompt": "new question", "project_id": p["id"]}
    )
    assert st == 200
    assert api.projects.item_ids(p["id"], "session") == [r["id"]]
    st, _ = call("POST", "/api/research", {"prompt": "x", "project_id": 9999})
    assert st == 400


def test_source_membership_raises_effective_level(app):  # noqa: F811
    call, api = app["call"], app["api"]
    from deepresearch.sources import DataSource

    api.sources.add(
        DataSource(
            name="phi-data",
            kind="web",
            uri="https://example.org",
            protection_level="P4",
        )
    )
    p = api.projects.create("Clinical")
    st, _ = call(
        "POST",
        f"/api/projects/{p['id']}/items",
        {"kind": "source", "ids": ["phi-data"]},
    )
    assert st == 200
    st, got = call("GET", f"/api/projects/{p['id']}")
    assert got["effective_level"] == "P4" and got["sources"][0]["name"] == "phi-data"
    st, _ = call(
        "POST", f"/api/projects/{p['id']}/items", {"kind": "source", "ids": ["nope"]}
    )
    assert st == 400
    # deleting the source forgets the membership
    src = api.sources.get("phi-data")
    call("DELETE", f"/api/sources/{src.id}")
    assert api.projects.item_ids(p["id"], "source") == []


def test_deleting_report_purges_membership(app):  # noqa: F811
    call, api = app["call"], app["api"]
    sid = _seed(api)
    p = api.projects.create("P")
    api.projects.add_item(p["id"], "session", sid)
    call("DELETE", f"/api/sessions/{sid}")
    assert api.projects.item_ids(p["id"], "session") == []


def test_lab_create_uses_project_partition(app, monkeypatch):  # noqa: F811
    call, api = app["call"], app["api"]
    sid = _seed(api)
    p = api.projects.create("P", lab_partition="gpu-a100")
    api.projects.add_item(p["id"], "session", sid)
    seen = {}
    monkeypatch.setattr(api.lab, "targets", {"x": object()})

    def fake_create(session_id, scope, text, request, target, data_sources=None):
        seen.update(request=request, target=target)
        return {"id": 1, "target": None, "session_id": session_id}

    monkeypatch.setattr(api.lab, "create", fake_create)
    monkeypatch.setattr(api.lab, "make_plan", lambda *a: None)
    monkeypatch.setattr(api, "_lab_view", lambda r: r)
    st, _ = call("POST", f"/api/sessions/{sid}/lab", {"scope": "document"})
    assert st == 200 and "gpu-a100" in seen["request"]


# ---------------------------------------------------------------- suggestions


def _embed(api, sid, vec, tags=None):
    api.sessions.update_embedding(sid, vec)
    if tags:
        api.store.set_meta(sid, tags=tags)


def test_suggestions_group_similar_and_tags(app):  # noqa: F811
    call, api = app["call"], app["api"]
    ids = [_seed(api, prompt=f"coral fertilization study {i}") for i in range(3)]
    other = [_seed(api, prompt=f"slurm scheduler tuning {i}") for i in range(3)]
    for i, s in enumerate(ids):
        _embed(api, s, _vec(1.0, 0.05 * i, 0.0))
    for i, s in enumerate(other):
        _embed(api, s, _vec(0.0, 0.05 * i, 1.0))
    api.store.set_meta(other[0], tags=["hpc"])
    api.store.set_meta(other[1], tags=["hpc"])
    st, out = call("GET", "/api/projects/suggestions")
    assert st == 200
    tag = next(g for g in out["groups"] if g["source"] == "your tag")
    assert tag["label"] == "hpc" and set(tag["sessions"]) == set(other[:2])
    sim = [g for g in out["groups"] if g["source"] == "similar reports"]
    assert {frozenset(g["sessions"]) for g in sim} == {frozenset(ids), frozenset(other)}
    coral = next(g for g in sim if set(g["sessions"]) == set(ids))
    assert "Coral" in coral["label"]
    # accept only what was ticked
    st, p = call(
        "POST",
        "/api/projects/suggestions/accept",
        {"title": "Coral", "sessions": ids[:2]},
    )
    assert st == 200 and sorted(api.projects.item_ids(p["id"], "session")) == sorted(
        ids[:2]
    )
    st, sim2 = call("GET", f"/api/projects/{p['id']}/similar")
    assert sim2["sessions"][0]["id"] == ids[2]


def test_label_for_uses_shared_distinctive_words():
    label = pj.label_for(
        ["Coral reef bleaching", "coral reef recovery", "reef coral spawning"]
    )
    assert set(label.split()) == {"Coral", "Reef"}
    corpus = ["HPC news Jan 2", "HPC news Jan 3", "HPC news Jan 4", "HPC tips", "reef"]
    label = pj.label_for(corpus[:3], corpus)
    assert "Jan" not in label and "News" in label and "HPC" in label


# ---------------------------------------------------------------- AI with fakes


class FakeModels:
    def __init__(self):
        self.prompts = []

    def generate_content(self, model, contents, config=None):
        self.prompts.append(contents)
        return SimpleNamespace(
            text="**Bottom line** it works *Session #1*",
            usage_metadata=SimpleNamespace(
                prompt_token_count=1000,
                candidates_token_count=100,
                thoughts_token_count=0,
            ),
        )

    def embed_content(self, model, contents):
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[1.0, 0.0, 0.0])])


def _fake_client(monkeypatch, api):
    fake = SimpleNamespace(models=FakeModels())
    monkeypatch.setattr(api.fx, "_client", lambda: fake)
    monkeypatch.setattr(api.fx, "_model", lambda: "fake-flash")
    monkeypatch.setattr(api, "_genai", lambda: fake)
    return fake


def test_summary_saved_and_goes_stale(app, monkeypatch):  # noqa: F811
    call, api = app["call"], app["api"]
    fake = _fake_client(monkeypatch, api)
    sid = _seed(api, prompt="Question one", result="Finding A. Finding B.")
    p = api.projects.create("P")
    api.projects.add_item(p["id"], "session", sid)
    st, out = call("POST", f"/api/projects/{p['id']}/summary")
    assert st == 200 and "Bottom line" in out["summary"] and out["cost_usd"] > 0
    assert (
        "Session #" in fake.models.prompts[-1]
        and "Finding A" in fake.models.prompts[-1]
    )
    st, got = call("GET", f"/api/projects/{p['id']}")
    assert got["project"]["summary"].startswith("**Bottom line**")
    assert got["summary_stale"] is False
    # a new report makes the summary stale
    s2 = _seed(api, prompt="Question two")
    with api.projects._conn() as c:
        c.execute("UPDATE project_items SET added_at = '2999-01-01T00:00:00'")
    api.projects.add_item(p["id"], "session", s2)
    st, got = call("GET", f"/api/projects/{p['id']}")
    assert got["summary_stale"] is True


def test_summary_needs_finished_reports(app):  # noqa: F811
    call, api = app["call"], app["api"]
    p = api.projects.create("Empty")
    st, out = call("POST", f"/api/projects/{p['id']}/summary")
    assert st == 400


def test_ask_is_scoped_to_project(app, monkeypatch):  # noqa: F811
    call, api = app["call"], app["api"]
    fake = _fake_client(monkeypatch, api)
    inside = _seed(api, prompt="inside", result="The inside answer is 42.")
    outside = _seed(api, prompt="outside", result="Outside secret text.")
    api.sessions.update_embedding(inside, _vec(1.0, 0.0, 0.0))
    api.sessions.update_embedding(outside, _vec(1.0, 0.0, 0.0))
    p = api.projects.create("P")
    api.projects.add_item(p["id"], "session", inside)
    st, out = call(
        "POST", f"/api/projects/{p['id']}/ask", {"question": "what is the answer"}
    )
    assert st == 200
    assert [m["id"] for m in out["matches"]] == [inside]
    prompt = fake.models.prompts[-1]
    assert "inside answer" in prompt and "Outside secret" not in prompt


def test_project_brief_becomes_filed_notebook(app, monkeypatch):  # noqa: F811
    call, api = app["call"], app["api"]
    _fake_client(monkeypatch, api)
    sid = _seed(api)
    p = api.projects.create("P")
    api.projects.add_item(p["id"], "session", sid)
    st, out = call("POST", f"/api/projects/{p['id']}/brief", {"style": "grant"})
    assert st == 200 and out["notebook"]["title"].startswith("Grant section")
    assert api.projects.item_ids(p["id"], "notebook") == [out["notebook"]["id"]]
    st, _ = call("POST", f"/api/projects/{p['id']}/brief", {"style": "haiku"})
    assert st == 400


# ---------------------------------------------------------------- exports

REPORT = "Claim one [cite: 1] see [Nature paper](https://nature.com/a) and [2](https://x.org/2).\n\n**Sources:**\n1. [nature.com](https://nature.com/a)\n"


def _project_with_everything(api):
    sid = _seed(api, prompt="Coral question", result=REPORT)
    kid = _child(api, sid)
    api.store.create_annotation(sid, "Claim one", 0, "important", "amber")
    nb = api.store.create_notebook("Draft", "notebook text")
    p = api.projects.create("Coral Grant", description="NSF proposal", nexus_ref="G-1")
    api.projects.add_item(p["id"], "session", sid)
    api.projects.add_item(p["id"], "notebook", nb["id"])
    return p, sid, kid


def test_export_markdown_dossier(app):  # noqa: F811
    call, api = app["call"], app["api"]
    p, sid, kid = _project_with_everything(api)
    st, out = call("GET", f"/api/projects/{p['id']}/export?format=md")
    md = out["content"]
    assert st == 200 and out["filename"] == "coral-grant.md"
    assert md.startswith("# Coral Grant") and "Nexus: G-1" in md
    assert f"Session #{sid}" in md and f"Session #{kid}" in md
    assert "> Claim one" in md and "important" in md and "notebook text" in md


def test_export_bibtex_and_csv(app):  # noqa: F811
    call, api = app["call"], app["api"]
    p, sid, _ = _project_with_everything(api)
    st, out = call("GET", f"/api/projects/{p['id']}/export?format=bib")
    assert "@misc{" in out["content"] and "https://nature.com/a" in out["content"]
    assert "x.org/2" not in out["content"]  # numeric labels are citation chips, skipped
    st, out = call("GET", f"/api/projects/{p['id']}/export?format=csv")
    assert out["content"].splitlines()[0] == "label,url,sessions"


def test_export_zip_package(app):  # noqa: F811
    call, api = app["call"], app["api"]
    p, sid, _ = _project_with_everything(api)
    st, raw = call("GET", f"/api/projects/{p['id']}/export?format=zip")
    assert st == 200
    z = zipfile.ZipFile(io.BytesIO(raw))
    names = z.namelist()
    root = "coral-grant/"
    for n in (
        "README.md",
        "dossier.md",
        "project.json",
        "citations.bib",
        "ro-crate-metadata.json",
        "reports/Index.md",
    ):
        assert root + n in names, n
    crate = json.loads(z.read(root + "ro-crate-metadata.json"))
    parts = {x["@id"] for x in crate["@graph"][1]["hasPart"]}
    assert "dossier.md" in parts
    bundle = json.loads(z.read(root + "project.json"))
    assert all("embedding" not in r for r in bundle["reports"])
    assert any(n.startswith(root + "notebooks/") for n in names)


def test_export_bad_format(app):  # noqa: F811
    call, api = app["call"], app["api"]
    p = api.projects.create("P")
    st, _ = call("GET", f"/api/projects/{p['id']}/export?format=docx")
    assert st == 400


def test_zip_includes_small_lab_outputs_and_skips_big_ones(app, monkeypatch):  # noqa: F811
    call, api = app["call"], app["api"]
    sid = _seed(api)
    p = api.projects.create("Lab project")
    api.projects.add_item(p["id"], "session", sid)
    run = {
        "id": 7,
        "session_id": sid,
        "status": "completed",
        "plan": {"title": "Allee test"},
        "verdict": {"pass": True, "checks": [{"name": "k", "pass": True, "got": 1}]},
        "result_md": "## Write-up",
        "files": [
            {"path": "outputs/verdict.json", "size": 20},
            {"path": "outputs/big.csv", "size": 50 * 1024**2},
            {"path": "outputs/model.bin", "size": 10},
        ],
    }
    folder = app["tmp"] / "lab" / "run_7" / "outputs"
    folder.mkdir(parents=True)
    (folder / "verdict.json").write_text('{"pass": true}')
    monkeypatch.setattr(api.lab, "all_runs", lambda limit=500: [run])
    monkeypatch.setattr(api.lab, "results_dir", app["tmp"] / "lab")
    st, raw = call("GET", f"/api/projects/{p['id']}/export?format=zip")
    names = zipfile.ZipFile(io.BytesIO(raw)).namelist()
    assert "lab-project/lab/run_7/writeup.md" in names
    assert "lab-project/lab/run_7/outputs/verdict.json" in names
    assert not any("big.csv" in n or "model.bin" in n for n in names)
    st, got = call("GET", f"/api/projects/{p['id']}")
    assert got["lab_runs"][0]["verdict"] == "passed (1/1 checks)"
    st, out = call("GET", f"/api/projects/{p['id']}/export?format=md")
    assert "Lab run #7: Allee test" in out["content"] and "[PASS] k" in out["content"]
