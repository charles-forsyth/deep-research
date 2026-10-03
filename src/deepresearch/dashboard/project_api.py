"""HTTP handlers for projects (docs/SPEC.md section 22).

Mixed into `Api`; uses its helpers (_session, _config, sources, store, lab, fx,
_genai, spawn) and adds routes in `register_project_routes`.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import traceback

from deepresearch.dashboard import projects as pj
from deepresearch.dashboard.claims import board as claims_board


class ProjectApi:
    # attributes provided by Api
    db_path: str
    projects: pj.ProjectStore

    def register_project_routes(self) -> None:
        r = self._route  # type: ignore[attr-defined]
        r("GET", r"/api/projects", self.projects_list)
        r("POST", r"/api/projects", self.projects_create)
        r("GET", r"/api/projects/inbox", self.projects_inbox)
        r("GET", r"/api/projects/suggestions", self.projects_suggestions)
        r("POST", r"/api/projects/suggestions/accept", self.projects_accept)
        r("POST", r"/api/projects/suggestions/name", self.projects_name_groups)
        r("GET", r"/api/projects/(\d+)", self.projects_get)
        r("PATCH", r"/api/projects/(\d+)", self.projects_patch)
        r("DELETE", r"/api/projects/(\d+)", self.projects_delete)
        r("POST", r"/api/projects/(\d+)/items", self.projects_add_items)
        r("DELETE", r"/api/projects/(\d+)/items", self.projects_remove_items)
        r("POST", r"/api/projects/(\d+)/home", self.projects_set_home)
        r("GET", r"/api/projects/(\d+)/similar", self.projects_similar)
        r("POST", r"/api/projects/(\d+)/summary", self.projects_summary)
        r("POST", r"/api/projects/(\d+)/ask", self.projects_ask)
        r("POST", r"/api/projects/(\d+)/brief", self.projects_brief)
        r("POST", r"/api/projects/(\d+)/audio", self.projects_audio)
        r("GET", r"/api/projects/(\d+)/export", self.projects_export)
        r("GET", r"/api/sessions/(\d+)/projects", self.session_projects)

    # ---- helpers ----------------------------------------------------------

    def _project(self, pid) -> dict:
        from deepresearch.dashboard.server import ApiError

        p = self.projects.get(int(pid))
        if not p:
            raise ApiError(404, f"Project {pid} not found")
        return p

    def _sessions_by_id(
        self, ids: list[int], with_embedding: bool = False
    ) -> list[dict]:
        if not ids:
            return []
        cols = "id, prompt, status, created_at, updated_at, parent_id, result" + (
            ", embedding" if with_embedding else ""
        )
        marks = ",".join("?" * len(ids))
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            rows = [
                dict(r)
                for r in conn.execute(
                    f"SELECT {cols} FROM sessions WHERE id IN ({marks}) ORDER BY id",
                    ids,
                )
            ]
        return rows

    def _project_sources(self, pid: int) -> list:
        out = []
        for sid in self.projects.item_ids(pid, "source"):
            s = self.sources.get(int(sid))  # type: ignore[attr-defined]
            if s:
                out.append(s)
        return out

    def _project_lab_runs(self, session_ids: list[int]) -> list[dict]:
        ids = set(session_ids)
        return [r for r in self.lab.all_runs(limit=5000) if r["session_id"] in ids]  # type: ignore[attr-defined]

    def _effective_level(self, p: dict, sources: list) -> str:
        return pj.strictest(
            [p["protection_level"], *(s.protection_level for s in sources)]
        )

    def _bundle(self, pid: int) -> dict:
        """Everything a project holds, for the page and the exports."""
        p = self._project(pid)
        roots = self.projects.items(pid, "session")
        root_ids = [r["ref_id"] for r in roots]
        home = {r["ref_id"]: bool(r["is_home"]) for r in roots}
        all_ids = self.projects.session_ids(pid)
        rows = {r["id"]: r for r in self._sessions_by_id(all_ids)}
        reports = [rows[i] for i in root_ids if i in rows]
        children = [rows[i] for i in all_ids if i in rows and i not in set(root_ids)]
        sources = self._project_sources(pid)
        notebooks = [
            nb
            for i in self.projects.item_ids(pid, "notebook")
            if (nb := self.store.get_notebook(int(i)))  # type: ignore[attr-defined]
        ]
        anns = [
            a
            for a in self.store.list_annotations()  # type: ignore[attr-defined]
            if a["session_id"] in set(all_ids)
        ]
        lab_runs = self._project_lab_runs(all_ids)
        return {
            "project": p,
            "effective_level": self._effective_level(p, sources),
            "reports": reports,
            "home": home,
            "children": children,
            "sources": sources,
            "notebooks": notebooks,
            "annotations": anns,
            "lab_runs": lab_runs,
        }

    def _flash(self):
        return self.fx._client(), self.fx._model()  # type: ignore[attr-defined]

    # ---- CRUD -------------------------------------------------------------

    def projects_list(self, query, body):
        archived = (query.get("archived") or ["0"])[0] == "1"
        rows = self.projects.all(include_archived=archived)
        return {"projects": rows, "inbox": len(self.projects.inbox_ids())}

    def projects_create(self, query, body):
        from deepresearch.dashboard.server import ApiError

        b = dict(body or {})
        try:
            p = self.projects.create(b.pop("title", ""), **b)
            for sid in b.get("sessions") or []:
                self.projects.add_item(p["id"], "session", int(sid))
            for sid in b.get("sources") or []:
                self._add_source(p["id"], sid)
        except ValueError as e:
            raise ApiError(400, str(e)) from e
        return self.projects.get(p["id"])

    def projects_get(self, pid, query, body):
        b = self._bundle(int(pid))
        p = b["project"]
        run_state = {r["id"]: r["status"] for r in b["reports"] + b["children"]}
        mp = self.projects.membership_map()
        reports = []
        for r in b["reports"]:
            kids = [
                c for c in b["children"] if self.projects.root_of(c["id"]) == r["id"]
            ]
            reports.append(
                {
                    "id": r["id"],
                    "prompt": r["prompt"],
                    "status": r["status"],
                    "created_at": r["created_at"],
                    "result_chars": len(r.get("result") or ""),
                    "children": len(kids),
                    "is_home": b["home"].get(r["id"], False),
                    "also_in": [x for x in mp.get(r["id"], []) if x["id"] != p["id"]],
                    "annotations": sum(
                        1 for a in b["annotations"] if a["session_id"] == r["id"]
                    ),
                }
            )
        stale = bool(
            p.get("summary_at") and self.projects.last_change(p["id"]) > p["summary_at"]
        )
        cites = pj.citations(b["reports"] + b["children"])
        return {
            "project": p,
            "effective_level": b["effective_level"],
            "summary_stale": stale,
            "reports": reports,
            "running": sum(1 for s in run_state.values() if s == "running"),
            "sources": [
                {
                    "id": s.id,
                    "name": s.name,
                    "kind": s.kind,
                    "title": s.title,
                    "uri": s.uri,
                    "status": s.status,
                    "protection_level": s.protection_level,
                }
                for s in b["sources"]
            ],
            "notebooks": [
                {
                    "id": n["id"],
                    "title": n["title"],
                    "updated_at": n["updated_at"],
                    "chars": len(n.get("content") or ""),
                }
                for n in b["notebooks"]
            ],
            "annotations": [
                {
                    "id": a["id"],
                    "session_id": a["session_id"],
                    "quote": a["quote"][:600],
                    "note": a["note"],
                    "color": a["color"],
                    "created_at": a.get("created_at"),
                }
                for a in b["annotations"]
            ],
            "lab_runs": [
                {
                    "id": r["id"],
                    "session_id": r["session_id"],
                    "title": (r.get("plan") or {}).get("title") or "",
                    "status": r["status"],
                    "stage": r.get("stage"),
                    "verdict": pj.verdict_line(r),
                    "verdict_pass": (r.get("verdict") or {}).get("pass")
                    if isinstance(r.get("verdict"), dict)
                    else None,
                    "created_at": r.get("created_at"),
                }
                for r in b["lab_runs"]
            ],
            "citations": len(cites),
            "top_citations": cites[:12],
            "claims": claims_board(
                b["lab_runs"], {r["id"]: r for r in b["reports"] + b["children"]}
            ),
        }

    def projects_patch(self, pid, query, body):
        from deepresearch.dashboard.server import ApiError

        self._project(pid)
        b = dict(body or {})
        if (
            "lab_target" in b
            and b["lab_target"]
            and not self.lab.target(b["lab_target"])
        ):  # type: ignore[attr-defined]
            raise ApiError(400, f"No Lab target named '{b['lab_target']}'")
        try:
            return self.projects.update(int(pid), **b)
        except ValueError as e:
            raise ApiError(400, str(e)) from e

    def projects_delete(self, pid, query, body):
        self._project(pid)
        self.projects.delete(int(pid))
        return {"deleted": int(pid)}

    def _add_source(self, pid: int, ref) -> None:
        s = self.sources.get(ref)  # type: ignore[attr-defined]
        if not s:
            raise ValueError(f"data source '{ref}' does not exist")
        self.projects.add_item(pid, "source", int(s.id))

    def projects_add_items(self, pid, query, body):
        from deepresearch.dashboard.server import ApiError

        self._project(pid)
        b = body or {}
        kind = b.get("kind") or "session"
        ids = b.get("ids") or ([b["id"]] if b.get("id") is not None else [])
        if not ids:
            raise ApiError(400, "Nothing to add")
        added = []
        try:
            for ref in ids:
                if kind == "source":
                    self._add_source(int(pid), ref)
                elif kind == "session":
                    self._session(str(int(ref)))  # type: ignore[attr-defined]
                    added.append(
                        self.projects.add_item(
                            int(pid), "session", int(ref), home=bool(b.get("home"))
                        )["id"]
                    )
                    continue
                elif kind == "notebook":
                    if not self.store.get_notebook(int(ref)):  # type: ignore[attr-defined]
                        raise ValueError(f"notebook {ref} does not exist")
                    self.projects.add_item(int(pid), "notebook", int(ref))
                else:
                    raise ValueError("kind must be session, source or notebook")
                added.append(ref)
        except (ValueError, KeyError) as e:
            raise ApiError(400, str(e)) from e
        return {"added": added, "project": self.projects.get(int(pid))}

    def projects_remove_items(self, pid, query, body):
        from deepresearch.dashboard.server import ApiError

        self._project(pid)
        b = body or {}
        kind = b.get("kind") or "session"
        ids = b.get("ids") or ([b["id"]] if b.get("id") is not None else [])
        if kind not in pj.KINDS:
            raise ApiError(400, "kind must be session, source or notebook")
        removed = []
        for ref in ids:
            if kind == "source":
                s = self.sources.get(ref)  # type: ignore[attr-defined]
                if not s:
                    continue
                ref = s.id
            if self.projects.remove_item(int(pid), kind, int(ref)):
                removed.append(ref)
        return {"removed": removed}

    def projects_set_home(self, pid, query, body):
        from deepresearch.dashboard.server import ApiError

        self._project(pid)
        sid = int((body or {}).get("session_id") or 0)
        self._session(str(sid))  # type: ignore[attr-defined]
        try:
            self.projects.set_home(int(pid), sid)
        except KeyError as e:
            raise ApiError(404, "Project not found") from e
        return {"projects": self.projects.projects_for("session", sid)}

    def session_projects(self, sid, query, body):
        self._session(sid)  # type: ignore[attr-defined]
        home = self.projects.home_project(int(sid))
        defaults = None
        if home:
            defaults = self._defaults(home)
        return {
            "root": self.projects.root_of(int(sid)),
            "projects": self.projects.projects_for("session", int(sid)),
            "defaults": defaults,
        }

    def _defaults(self, p: dict) -> dict:
        srcs = self._project_sources(p["id"])
        return {
            "project_id": p["id"],
            "title": p["title"],
            "data_sources": [s.name for s in srcs],
            "lab_target": p.get("lab_target") or "",
            "lab_partition": p.get("lab_partition") or "",
            "protection_level": self._effective_level(p, srcs),
        }

    # ---- inbox and sorting ------------------------------------------------

    def projects_inbox(self, query, body):
        ids = self.projects.inbox_ids()
        return {"sessions": ids, "count": len(ids)}

    def projects_suggestions(self, query, body):
        """Groups of unfiled reports that look like one project (embeddings + tags)."""
        inbox = set(self.projects.inbox_ids())
        rows = [
            r
            for r in self._sessions_by_id(sorted(inbox), with_embedding=True)
            if r["status"] == "completed"
        ]
        thr = float((query.get("threshold") or ["0.78"])[0])
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            corpus = [
                r[0] or ""
                for r in conn.execute(
                    "SELECT prompt FROM sessions WHERE parent_id IS NULL"
                )
            ]
        groups = pj.suggest_groups(
            rows, threshold=max(0.6, min(thr, 0.95)), corpus=corpus
        )
        for g in groups:
            g["source"] = "similar reports"
        # tags the user already applied by hand are the strongest signal
        by_tag: dict[str, list[int]] = {}
        with sqlite3.connect(self.db_path, timeout=10) as conn:
            for sid, tags in conn.execute("SELECT session_id, tags FROM session_meta"):
                if sid in inbox:
                    for t in pj._loads(tags, []):
                        by_tag.setdefault(t, []).append(sid)
        prompts = {r["id"]: r["prompt"] for r in self._sessions_by_id(sorted(inbox))}
        tag_groups = [
            {
                "label": t,
                "sessions": sorted(ids, reverse=True),
                "prompts": {s: pj.one_line(prompts.get(s, ""), 140) for s in ids},
                "source": "your tag",
                "cohesion": None,
            }
            for t, ids in sorted(by_tag.items(), key=lambda kv: -len(kv[1]))
            if len(ids) >= 2
        ]
        existing = {p["title"].lower(): p["id"] for p in self.projects.all()}
        out = tag_groups + groups
        for g in out:
            g["existing_project"] = existing.get(g["label"].lower())
        return {"groups": out, "inbox": len(inbox), "embedded": len(rows)}

    def projects_accept(self, query, body):
        """Create (or reuse) a project from a suggestion. Only what the user ticked."""
        from deepresearch.dashboard.server import ApiError

        b = body or {}
        ids = [int(x) for x in b.get("sessions") or []]
        if not ids:
            raise ApiError(400, "Pick at least one report")
        try:
            if b.get("project_id"):
                p = self._project(b["project_id"])
            else:
                p = self.projects.create(b.get("title") or "Untitled project")
            for sid in ids:
                self.projects.add_item(p["id"], "session", sid)
        except ValueError as e:
            raise ApiError(400, str(e)) from e
        return self.projects.get(p["id"])

    def projects_name_groups(self, query, body):
        """One Flash call names every suggested group (optional; heuristics are free)."""
        from deepresearch.dashboard.server import ApiError

        groups = (body or {}).get("groups") or []
        if not groups or len(groups) > 40:
            raise ApiError(400, "Send 1-40 groups")
        lines = []
        for i, g in enumerate(groups):
            prompts = [pj.one_line(str(p), 120) for p in (g.get("prompts") or [])[:8]]
            lines.append(f"GROUP {i}:\n- " + "\n- ".join(prompts))
        prompt = (
            "Each group below is a set of research questions that belong together. Give "
            "each group a short project name (2-5 words, Title Case, no quotes), the way a "
            "researcher would name a folder for a grant, paper or topic. Reply with JSON "
            'only: {"names": ["...", ...]} in group order.\n\n' + "\n\n".join(lines)
        )
        try:
            client, model = self._flash()
            resp = client.models.generate_content(model=model, contents=prompt)
            m = re.search(r"\{.*\}", resp.text or "", re.S)
            names = json.loads(m.group(0))["names"] if m else []
        except Exception as e:
            raise ApiError(502, f"Naming failed: {e}") from e
        names = [" ".join(str(n).split())[:80] for n in names][: len(groups)]
        return {"names": names, "cost_usd": pj.flash_cost(resp)}

    def projects_similar(self, pid, query, body):
        members = self._sessions_by_id(
            self.projects.item_ids(int(pid), "session"), True
        )
        cands = [
            r
            for r in self._sessions_by_id(self.projects.inbox_ids(), True)
            if r["status"] == "completed"
        ]
        return {"sessions": pj.similar_to(members, cands)}

    # ---- AI: summary, ask, brief, audio -----------------------------------

    def projects_summary(self, pid, query, body):
        from deepresearch.dashboard.server import ApiError

        b = self._bundle(int(pid))
        reports = [r for r in b["reports"] if (r.get("result") or "").strip()]
        if not reports:
            raise ApiError(400, "The project has no finished reports to summarize yet")
        prompt = pj.summary_prompt(b["project"], reports, b["lab_runs"])
        try:
            client, model = self._flash()
            resp = client.models.generate_content(model=model, contents=prompt)
        except ApiError:
            raise
        except Exception as e:
            raise ApiError(502, f"Summary failed: {e}") from e
        cost = pj.flash_cost(resp)
        p = self.projects.save_summary(int(pid), (resp.text or "").strip(), cost)
        return {
            "summary": p["summary"],
            "summary_at": p["summary_at"],
            "cost_usd": cost,
        }

    def projects_ask(self, pid, query, body):
        """Semantic search and an answer limited to this project's reports + sources."""
        from deepresearch.dashboard.server import ApiError

        p = self._project(pid)
        q = ((body or {}).get("question") or "").strip()
        if not q:
            raise ApiError(400, "Question is empty")
        ids = self.projects.session_ids(int(pid))
        docs = [
            d
            for d in self._sessions_by_id(ids, with_embedding=True)
            if d["status"] == "completed" and (d.get("result") or "").strip()
        ]
        sources = self._project_sources(int(pid))
        if not docs and not sources:
            raise ApiError(400, "Nothing in this project to search yet")
        try:
            client = self._genai()  # type: ignore[attr-defined]
            for d in docs:
                if d.get("embedding"):
                    continue
                resp = client.models.embed_content(
                    model="gemini-embedding-001",
                    contents=f"Objective: {d['prompt']}\n\nResult:\n{d['result']}"[
                        :15000
                    ],
                )
                if resp.embeddings:
                    vec = json.dumps(resp.embeddings[0].values)
                    self.sessions.update_embedding(d["id"], vec)  # type: ignore[attr-defined]
                    d["embedding"] = vec
            ranked: list[tuple[float, dict]] = []
            if docs:
                qv = client.models.embed_content(
                    model="gemini-embedding-001", contents=q
                )
                if not qv.embeddings or not qv.embeddings[0].values:
                    raise ApiError(502, "Embedding API returned no vector")
                ranked = pj.cosine_rank(
                    list(qv.embeddings[0].values), docs, pj.ASK_TOP_REPORTS
                )
            sources_ctx = ""
            if sources:
                from deepresearch.sources.usage import ask_context

                sources_ctx = ask_context(sources)
            fclient, model = self._flash()
            resp = fclient.models.generate_content(
                model=model, contents=pj.ask_prompt(q, p, ranked, sources_ctx)
            )
        except ApiError:
            raise
        except Exception as e:
            raise ApiError(502, f"Ask failed: {e}") from e
        self.projects.touch(int(pid))
        return {
            "answer": resp.text,
            "matches": [
                {"id": d["id"], "score": round(sc, 4), "prompt": d["prompt"]}
                for sc, d in ranked
            ],
            "sources": [s.name for s in sources],
            "cost_usd": pj.flash_cost(resp),
        }

    def _project_text(self, pid: int) -> tuple[str, str]:
        b = self._bundle(pid)
        p = b["project"]
        labs = pj.lab_findings(b["lab_runs"])
        lab_block = f"\n\n## Lab results\n\n{labs}" if labs else ""
        stale = bool(
            p.get("summary_at") and self.projects.last_change(p["id"]) > p["summary_at"]
        )
        if p.get("summary") and not stale:
            # the summary already weighs the Lab runs; add their write-ups so the
            # voice overview can say what each computation showed
            return p["title"], p["summary"] + lab_block
        budget = max(8000, 300_000 // max(1, len(b["reports"])))
        parts = [
            f"## Session #{r['id']}: {pj.one_line(r['prompt'], 200)}\n\n"
            + pj.strip_sources(r.get("result") or "")[:budget]
            for r in b["reports"]
        ]
        return p["title"], "\n\n".join(parts) + lab_block

    def projects_brief(self, pid, query, body):
        """Executive brief, slides, email, grant section, lay summary or literature
        review from the whole project, saved as a notebook filed in the project."""
        from deepresearch.dashboard.server import ApiError

        self._project(pid)
        style = (body or {}).get("style") or "brief"
        b = self._bundle(int(pid))
        reports = [r for r in b["reports"] if (r.get("result") or "").strip()]
        if not reports:
            raise ApiError(400, "Nothing to build from yet")
        content = pj.summary_prompt(b["project"], reports, b["lab_runs"]).split(
            "\nREPORTS:\n", 1
        )[-1]
        try:
            out = self.fx.brief(b["project"]["title"], content, style)  # type: ignore[attr-defined]
        except ValueError as e:
            raise ApiError(400, str(e)) from e
        except Exception as e:
            raise ApiError(502, f"Brief failed: {e}") from e
        label = self.fx.BRIEF_LABELS.get(style, style.title())  # type: ignore[attr-defined]
        nb = self.store.create_notebook(  # type: ignore[attr-defined]
            f"{label}: {b['project']['title']}"[:200],
            out["markdown"]
            + f'\n\n---\n*Built from project "{b["project"]["title"]}"*\n',
        )
        self.projects.add_item(int(pid), "notebook", nb["id"])
        return {"notebook": nb, "cost_usd": out.get("cost_usd")}

    def projects_audio(self, pid, query, body):
        """AI-voice overview of the project (uses its summary, or builds one)."""
        from deepresearch.dashboard.features import VOICES
        from deepresearch.dashboard.server import ApiError

        self._project(pid)
        voice = (body or {}).get("voice") or "Charon"
        if voice not in VOICES:
            raise ApiError(400, "bad voice")
        title, content = self._project_text(int(pid))
        if not content.strip():
            raise ApiError(400, "Nothing to read yet")
        with self._jobs_lock:  # type: ignore[attr-defined]
            self._job_seq += 1  # type: ignore[attr-defined]
            jid = self._job_seq  # type: ignore[attr-defined]
            self._jobs[jid] = {
                "id": jid,
                "status": "running",
                "result": None,
                "error": None,
            }  # type: ignore[attr-defined]

        fx = self.fx  # type: ignore[attr-defined]  # the request's workspace (see audio_start)

        def work():
            try:
                res = fx.make_audio("project", int(pid), title, content, "summary", voice)
                res.pop("path", None)
                self._jobs[jid].update(status="done", result=res)  # type: ignore[attr-defined]
            except Exception as e:
                traceback.print_exc()
                self._jobs[jid].update(status="error", error=str(e)[:300])  # type: ignore[attr-defined]

        threading.Thread(target=work, daemon=True).start()
        return {"job": jid}

    # ---- exports ----------------------------------------------------------

    def projects_export(self, pid, query, body):
        from deepresearch.dashboard.server import ApiError, RawResponse

        fmt = (query.get("format") or ["md"])[0]
        b = self._bundle(int(pid))
        p = b["project"]
        notes: dict[int, list[dict]] = {}
        for a in b["annotations"]:
            notes.setdefault(a["session_id"], []).append(a)
        sources = [
            {
                "name": s.name,
                "kind": s.kind,
                "uri": s.uri,
                "title": s.title,
                "protection_level": s.protection_level,
            }
            for s in b["sources"]
        ]
        reports = b["reports"] + b["children"]
        cites = pj.citations(reports)
        name = pj.slug(p["title"])
        dossier = pj.dossier_markdown(
            p,
            reports,
            notes,
            b["lab_runs"],
            sources,
            b["notebooks"],
            b["effective_level"],
        )
        if fmt == "md":
            return {"filename": f"{name}.md", "content": dossier}
        bundle = {
            "project": {k: v for k, v in p.items()},
            "effective_level": b["effective_level"],
            "reports": [
                {k: v for k, v in r.items() if k != "embedding"} for r in reports
            ],
            "annotations": b["annotations"],
            "lab_runs": [
                {
                    k: r.get(k)
                    for k in (
                        "id",
                        "session_id",
                        "status",
                        "plan",
                        "verdict",
                        "result_md",
                        "files",
                        "created_at",
                        "finished_at",
                    )
                }
                for r in b["lab_runs"]
            ],
            "data_sources": sources,
            "notebooks": b["notebooks"],
            "citations": cites,
        }
        if fmt == "json":
            return {"filename": f"{name}.json", "content": bundle}
        if fmt == "bib":
            return {"filename": f"{name}.bib", "content": pj.bibtex(p, cites)}
        if fmt == "csv":
            return {
                "filename": f"{name}_citations.csv",
                "content": pj.citations_csv(cites),
            }
        if fmt == "zip":
            lab_files: dict[str, bytes] = {}
            for run in b["lab_runs"]:
                base = f"run_{run['id']}"
                if run.get("result_md"):
                    lab_files[f"{base}/writeup.md"] = run["result_md"].encode()
                for f in run.get("files") or []:
                    path = str(f.get("path") or "")
                    size = int(f.get("size") or 0)
                    if size > 5 * 1024**2 or not re.search(
                        r"\.(json|csv|txt|md|png|jpg|log)$", path, re.I
                    ):
                        continue
                    try:
                        lab_files[f"{base}/{path}"] = self.lab.file_path(
                            run["id"], path
                        ).read_bytes()  # type: ignore[attr-defined]
                    except (FileNotFoundError, OSError):
                        continue
            audio_files: dict[str, bytes] = {}
            for kind, ref in [("project", p["id"])] + [
                ("session", r["id"]) for r in b["reports"]
            ]:
                for a in self.fx.list_audio(kind, ref)[:1]:  # type: ignore[attr-defined]
                    try:
                        data, _, fname = self.fx.audio_file(a["id"])  # type: ignore[attr-defined]
                        audio_files[fname] = data
                    except FileNotFoundError:
                        continue
            data = pj.research_package(
                p,
                dossier,
                bundle,
                reports,
                b["notebooks"],
                lab_files,
                audio_files,
                cites,
            )
            return RawResponse(data, "application/zip", f"{name}.zip", inline=False)
        raise ApiError(400, "format must be md, json, bib, csv or zip")
