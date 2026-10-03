/* Projects: the home screen. A project holds reports, data sources, notebooks and
   (through its reports) Lab runs and highlights, and remembers defaults for new work.
   Loaded before app.js; uses app.js helpers (api, esc, toast, MODAL, ...) at call time. */
"use strict";

const PROJ = {
  list: [],
  inbox: 0,
  colors: ["cyan", "amber", "magenta", "green", "red"],
  briefStyles: [["brief", "Executive brief"], ["slides", "Slide outline"], ["email", "Email"], ["grant", "Grant section"], ["lay", "Lay summary"], ["litreview", "Literature review"]],

  async load() {
    const r = await api("/api/projects");
    this.list = r.projects; this.inbox = r.inbox;
    this.renderStrip();
    return r;
  },
  byId(id) { return this.list.find((p) => p.id === +id); },
  dot(color) { return `<span class="pdot ${esc(color || "cyan")}" aria-hidden="true"></span>`; },
  pill(p) { return `<span class="projpill ${esc(p.color)}${p.is_home ? " home" : ""}" title="${esc(p.title)}${p.is_home ? " (home project: its defaults apply)" : " (also in this project)"}">${esc(clip(p.title, 28))}</span>`; },
  // archive rows: the home project, then "+N" for the rest (the filter already says which project)
  pills(list) {
    if (!list || !list.length) return "";
    const home = list.find((p) => p.is_home) || list[0];
    if (typeof S.project === "number" && list.length === 1) return "";
    return this.pill(home) + (list.length > 1 ? `<span class="projpill more" title="${esc(list.filter((p) => p !== home).map((p) => p.title).join(", "))}">+${list.length - 1}</span>` : "");
  },
  level(l) { return `<span class="lvl ${esc(l)}" title="Protection level (a label; never enforced)">${esc(l)}</span>`; },

  // ------------------------------------------------------------------ left pane
  renderStrip() {
    // v0.54.0: projects are chosen from the report filter menu; this strip stays as a
    // hidden container (kept for the old element id) and the filter chip is refreshed
    if (typeof renderFilterChips === "function") renderFilterChips();
    const el = $("#proj-strip"); if (!el || el.hidden) return;
    const cur = S.project || "all";
    const row = (key, label, count, extra = "") => `<button class="proj-row ${String(cur) === String(key) ? "on" : ""}" data-p="${esc(key)}">${extra}<span class="t">${esc(label)}</span><span class="n">${count ?? ""}</span></button>`;
    el.innerHTML = `
      <div class="proj-head"><span class="label">Projects</span>
        <span><button class="linkbtn" id="proj-sort" title="Group unfiled reports into projects">sort inbox</button>
        <button class="linkbtn" id="proj-new" title="New project">+ new</button></span></div>
      ${row("all", "All reports", "")}
      ${row("inbox", "Inbox", this.inbox, '<span class="pdot inbox" aria-hidden="true"></span>')}
      ${this.list.map((p) => row(p.id, p.title, p.counts.session, this.dot(p.color))).join("")}`;
    el.querySelectorAll("[data-p]").forEach((b) => {
      b.onclick = () => {
        const k = b.dataset.p;
        S.project = k === "all" ? null : k === "inbox" ? "inbox" : +k;
        this.renderStrip(); renderSessionList();
        if (typeof S.project === "number") this.open(S.project);
      };
    });
    el.querySelector("#proj-new").onclick = () => this.createDialog();
    el.querySelector("#proj-sort").onclick = () => this.openSorter();
  },
  matches(s) {
    if (!S.project) return true;
    if (s.parent_id) return false;
    if (S.project === "inbox") return !(s.projects || []).length;
    return (s.projects || []).some((p) => p.id === S.project);
  },

  // ------------------------------------------------------------------ tabs
  open(id) {
    const p = this.byId(id);
    openTab({ key: `p${id}`, kind: "project", id: +id, title: p ? p.title : `Project ${id}` });
  },
  openSorter() { openTab({ key: "sorter", kind: "sorter", title: "Sort inbox" }); },

  // ------------------------------------------------------------------ create / edit
  // ---- claims board (v0.45) -------------------------------------------------
  claimsCard(cb) {
    if (!cb || !cb.total) return "";
    const c = cb.counts;
    const chip = (k, label) => c[k] ? `<span class="outcome-badge ${k}">${c[k]} ${label}</span>` : "";
    const val = (x) => x == null ? "\u2013" : typeof x === "number" ? (Math.abs(x) >= 1000 || Number.isInteger(x) ? String(x) : (+x.toPrecision(4)).toString()) : clip(String(x), 90);
    const row = (cl) => {
      const main = cl.checks.filter((k) => k.kind === "claim");
      const rest = cl.checks.filter((k) => k.kind !== "claim");
      const line = (k) => `<li class="${k.pass === true ? "ok" : k.pass === false ? "bad" : ""}"><span class="mono">${esc(k.name)}</span> <span class="dim">expected</span> ${esc(val(k.expected))} <span class="dim">got</span> ${esc(val(k.got))}</li>`;
      const tries = cl.attempts.length > 1 ? ` \u00b7 ${cl.attempts.length} attempts` : "";
      return `<details class="claim ${esc(cl.outcome)}">
        <summary><span class="outcome-badge ${esc(cl.outcome)}">${esc(cl.outcome)}</span>
          <span class="claim-q">${esc(clip(oneLine(cl.question), 220))}</span>
          <span class="mono dim claim-meta">Lab #${cl.run_id} \u00b7 report #${cl.session_id}${tries}</span></summary>
        <div class="claim-body">
          <div class="dim" style="font-size:12px">${esc(cl.why)}</div>
          ${main.length ? `<ul class="claim-checks">${main.map(line).join("")}</ul>` : ""}
          ${rest.length ? `<div class="dim" style="font-size:11px;margin-top:4px">Checks that make the result trustworthy</div><ul class="claim-checks minor">${rest.map(line).join("")}</ul>` : ""}
          <div style="margin-top:6px"><a href="#" data-open="${cl.session_id}" class="linkbtn">Open the report</a>${cl.attempts.length > 1 ? ` <span class="dim" style="font-size:11px">\u00b7 attempts: ${cl.attempts.map((a) => `#${a.run_id} ${esc(a.outcome)}`).join(", ")}</span>` : ""}</div>
        </div></details>`;
    };
    return `<div class="card proj-claims">
      <div class="card-h"><h3>Claims tested</h3><span class="grow"></span>${chip("confirmed", "confirmed")}${chip("refuted", "refuted")}${chip("inconclusive", "inconclusive")}${chip("pending", "pending")}${chip("broken", "broken")}</div>
      <div class="dim" style="font-size:11.5px;margin-bottom:4px">What the Lab runs checked in these reports. Refuted claims come first: they are the ones to correct or discuss.</div>
      ${cb.claims.map(row).join("")}
    </div>`;
  },

  settingsForm(p = {}) {
    const tgt = (LAB.targetsList || [])[0];
    const parts = Object.entries(tgt?.partitions || {});
    return `
      <div class="field"><label for="pj-title">Title</label><input id="pj-title" value="${esc(p.title || "")}" placeholder="e.g. NSF CAREER 2027, Coral thesis, CephRDS paper" maxlength="120"></div>
      <div class="field"><label for="pj-desc">Description <span class="dim">(optional; the AI summary reads it)</span></label><textarea id="pj-desc" rows="2">${esc(p.description || "")}</textarea></div>
      <div class="row">
        <div class="field"><label>Color</label><div class="pj-colors">${this.colors.map((c) => `<button type="button" class="pdot big ${c} ${c === (p.color || "cyan") ? "on" : ""}" data-c="${c}" aria-label="${c}"></button>`).join("")}</div></div>
        <div class="field"><label for="pj-level">Protection level</label><select id="pj-level">${["P1", "P2", "P3", "P4"].map((l) => `<option ${l === (p.protection_level || "P2") ? "selected" : ""}>${l}</option>`).join("")}</select></div>
        <div class="field"><label for="pj-nexus">Nexus grant / lab id <span class="dim">(text only)</span></label><input id="pj-nexus" value="${esc(p.nexus_ref || "")}" placeholder="optional"></div>
      </div>
      <div class="row">
        <div class="field"><label for="pj-part">Lab partition default</label><select id="pj-part"><option value="">cluster default</option>${parts.map(([k, v]) => `<option value="${esc(k)}" ${k === p.lab_partition ? "selected" : ""}>${esc(k)}${v.gpus ? " + " + v.gpus + " GPU" : ""}${v.spot ? " (spot)" : ""}</option>`).join("")}</select></div>
        <div class="field"><label>Data sources</label><div class="dim" style="font-size:11.5px;padding-top:6px">Add them on the project page; new research, Ask and Lab runs start with them.</div></div>
      </div>
      <div class="dim" style="font-size:11.5px">The protection level is a label for you and your collaborators. The project shows the strictest of this and its data sources' levels.</div>`;
  },
  readForm() {
    return {
      title: $("#pj-title").value.trim(), description: $("#pj-desc").value.trim(),
      color: $("#modal .pj-colors .on")?.dataset.c || "cyan", protection_level: $("#pj-level").value,
      nexus_ref: $("#pj-nexus").value.trim(), lab_partition: $("#pj-part").value,
      lab_target: ((LAB.targetsList || [])[0] || {}).name || "",
    };
  },
  wireColors() { $$("#modal .pj-colors [data-c]").forEach((b) => (b.onclick = () => $$("#modal .pj-colors [data-c]").forEach((x) => x.classList.toggle("on", x === b)))); },
  createDialog(prefill = {}) {
    return new Promise((resolve) => {
      MODAL.open(`<h3>New project</h3><div class="dim">One per grant, paper, proposal or thesis. Reports can sit in more than one project.</div>${this.settingsForm(prefill)}
        <div class="acts"><button class="btn" data-x="0">Cancel</button><button class="btn primary" data-x="1">Create project</button></div>`, { onClose: () => resolve(null) });
      this.wireColors(); $("#pj-title").focus();
      $('#modal [data-x="0"]').onclick = () => MODAL.close();
      const go = $('#modal [data-x="1"]');
      go.onclick = () => busy(go, async () => {
        const f = this.readForm(); if (!f.title) { $("#pj-title").focus(); return toast("Give the project a title", "err"); }
        const p = await api("/api/projects", { method: "POST", body: { ...f, sessions: prefill.sessions || [] } });
        await this.load(); loadSessions().catch(() => {});
        toast(`Project \u201c${p.title}\u201d created`, "ok");
        const cb = MODAL.onClose; MODAL.onClose = null; MODAL.close(); if (cb) resolve(p);
        if (!prefill.noOpen) this.open(p.id);
      });
    });
  },
  editDialog(p, after) {
    MODAL.open(`<h3>Project settings</h3>${this.settingsForm(p)}
      <div class="acts"><button class="btn danger" data-x="del" style="margin-right:auto">Delete project</button><button class="btn" data-x="0">Cancel</button><button class="btn primary" data-x="1">Save</button></div>`);
    this.wireColors();
    $('#modal [data-x="0"]').onclick = () => MODAL.close();
    const go = $('#modal [data-x="1"]');
    go.onclick = () => busy(go, async () => {
      await api(`/api/projects/${p.id}`, { method: "PATCH", body: this.readForm() });
      MODAL.close(); await this.load(); loadSessions().catch(() => {}); toast("Saved", "ok"); after && after();
    });
    $('#modal [data-x="del"]').onclick = async () => {
      if (!(await confirmBox(`Delete \u201c${p.title}\u201d?`, "Only the project goes. Its reports, data sources, notebooks and Lab runs stay; reports with no other project return to the Inbox.", "Delete project"))) return;
      await api(`/api/projects/${p.id}`, { method: "DELETE" });
      if (S.project === p.id) S.project = null;
      await this.load(); loadSessions().catch(() => {}); closeTab(`p${p.id}`); toast("Project deleted", "ok");
    };
  },

  // ------------------------------------------------------------------ file a report
  async fileDialog(s) {
    await this.load();
    const root = s.parent_id ? (await api(`/api/sessions/${s.id}/projects`)).root : s.id;
    const cur = new Map((s.projects || []).map((p) => [p.id, p.is_home]));
    MODAL.open(`<h3>Projects for report #${root}</h3>
      ${root !== s.id ? `<div class="dim">This is a sub-report; it follows its top-level report #${root}.</div>` : `<div class="dim">Tick every project this report belongs to. The <b>home</b> project supplies its defaults (data sources, Lab partition, level).</div>`}
      <div class="pj-file">${this.list.map((p) => `<label class="pj-file-row"><input type="checkbox" value="${p.id}" ${cur.has(p.id) ? "checked" : ""}> ${this.dot(p.color)} <span class="t">${esc(p.title)}</span>
        <span class="grow"></span><span class="dim" style="font-size:11px">home</span><input type="radio" name="pj-home" value="${p.id}" ${cur.get(p.id) ? "checked" : ""} aria-label="Home project"></label>`).join("") || '<div class="dim">No projects yet.</div>'}</div>
      <button class="linkbtn" id="pj-file-new">+ new project</button>
      <div class="acts"><button class="btn" data-x="0">Cancel</button><button class="btn primary" data-x="1">Save</button></div>`);
    $('#modal [data-x="0"]').onclick = () => MODAL.close();
    $("#pj-file-new").onclick = async () => { const p = await this.createDialog({ sessions: [root], noOpen: true }); if (p) { delete S.cache[s.id]; renderStage(); } };
    const go = $('#modal [data-x="1"]');
    go.onclick = () => busy(go, async () => {
      const want = new Set($$('#modal .pj-file input[type="checkbox"]:checked').map((x) => +x.value));
      const home = +($('#modal input[name="pj-home"]:checked')?.value || 0);
      for (const id of want) if (!cur.has(id)) await api(`/api/projects/${id}/items`, { method: "POST", body: { kind: "session", ids: [root] } });
      for (const id of cur.keys()) if (!want.has(id)) await api(`/api/projects/${id}/items`, { method: "DELETE", body: { kind: "session", ids: [root] } });
      if (home && want.has(home)) await api(`/api/projects/${home}/home`, { method: "POST", body: { session_id: root } });
      MODAL.close(); delete S.cache[s.id]; delete S.cache[root];
      await Promise.all([this.load(), loadSessions()]); renderStage(); toast("Projects updated", "ok");
    });
  },

  // ------------------------------------------------------------------ project page
  async render(v, t) {
    v.innerHTML = `<div class="pad"><div class="empty-result scan">Loading project\u2026</div></div>`;
    let d;
    try { d = await api(`/api/projects/${t.id}`); }
    catch (e) { if (!stale(v)) v.innerHTML = `<div class="pad"><div class="err-banner">${esc(e.message)}</div></div>`; return; }
    if (stale(v)) return;
    const p = d.project;
    if (t.title !== p.title) { t.title = p.title; saveTabs(); renderTabs(); }
    const passed = d.lab_runs.filter((r) => r.verdict_pass === true).length;
    const failed = d.lab_runs.filter((r) => r.verdict_pass === false).length;
    v.innerHTML = `
    <div class="pad proj-page">
      <div class="proj-hero ${esc(p.color)}">
        <div class="proj-title-row">
          <h2>${esc(p.title)}</h2>
          ${this.level(d.effective_level)}
          ${p.nexus_ref ? `<span class="chip" title="Nexus reference (text only)">NEXUS <b>${esc(p.nexus_ref)}</b></span>` : ""}
          <span class="grow"></span>
          <button class="btn small" data-a="settings">Settings</button>
        </div>
        ${p.description ? `<p class="dim">${esc(p.description)}</p>` : ""}
        <div class="stat-grid proj-stats">
          <div class="stat"><div class="v">${d.reports.length}</div><div class="k">reports</div></div>
          <div class="stat"><div class="v">${d.sources.length}</div><div class="k">data sources</div></div>
          <div class="stat" title="${passed} passed, ${failed} failed their checks"><div class="v">${d.lab_runs.length}</div><div class="k">lab runs${d.lab_runs.length ? ` \u00b7 <span class="ok">${passed} pass</span>` : ""}</div></div>
          <div class="stat"><div class="v">${d.notebooks.length}</div><div class="k">notebooks</div></div>
          <div class="stat"><div class="v">${d.annotations.length}</div><div class="k">highlights</div></div>
          <div class="stat"><div class="v">${d.citations}</div><div class="k">cited</div></div>
        </div>
        <div class="proj-actions">
          <button class="btn primary" data-a="research">+ New research here</button>
          <button class="btn" data-a="add">Add reports</button>
          ${typeof WSUI !== "undefined" && WSUI.enabled ? `<button class="btn" data-a="ws-copy" title="Copy this project with its reports, notes, Lab runs and data sources into another workspace">\u29C9 Copy to workspace\u2026</button>` : ""}
          <select class="btn" data-a="export" aria-label="Export project">
            <option value="">Export\u2026</option>
            <optgroup label="Documents"><option value="md">Dossier (Markdown)</option><option value="html">Dossier (standalone HTML)</option><option value="print">Print / PDF</option></optgroup>
            <optgroup label="Research outputs"><option value="zip">Research package (.zip: Obsidian folder, Lab results, RO-Crate)</option><option value="bib">Citations (BibTeX)</option><option value="csv">Citations (CSV)</option><option value="json">Everything (JSON)</option></optgroup>
            <optgroup label="AI">${this.briefStyles.map(([k, l]) => `<option value="brief:${k}">${esc(l)}</option>`).join("")}<option value="audio">AI voice overview</option></optgroup>
          </select>
        </div>
      </div>

      <div class="card proj-summary">
        <div class="card-h"><h3>AI summary</h3>
          <span class="dim" style="font-size:11px">${p.summary_at ? `generated ${ago(p.summary_at)}${p.summary_cost != null ? ` \u00b7 $${(+p.summary_cost).toFixed(3)}` : ""}` : ""}</span>
          ${d.summary_stale ? '<span class="chip bad" title="Reports changed since the summary was written">OUT OF DATE</span>' : ""}
          <span class="grow"></span>
          <button class="btn small ${p.summary && !d.summary_stale ? "" : "primary"}" data-a="summary">${p.summary ? "\u21BB Regenerate" : "Generate summary"}</button></div>
        <div class="md" id="proj-summary">${p.summary ? "" : `<div class="dim">One page across every report and Lab run in the project: bottom line, key findings, agreements and conflicts, computational evidence, gaps and next steps. Gemini Flash, usually a few cents.</div>`}</div>
      </div>

      <div class="card proj-ask">
        <h3>Ask this project</h3>
        <div class="dim" style="font-size:11.5px;margin-bottom:6px">Searches only this project's ${d.reports.length} report${d.reports.length === 1 ? "" : "s"}${d.sources.length ? ` and ${d.sources.length} data source${d.sources.length === 1 ? "" : "s"}` : ""}, and answers with citations.</div>
        <div class="dock-inner"><textarea id="pa-q" rows="1" placeholder="e.g. What do these reports disagree about?"></textarea><button class="btn primary" data-a="ask">Ask</button></div>
        <div id="pa-out"></div>
      </div>

      ${this.claimsCard(d.claims)}

      <div class="proj-grid">
        <div class="card"><div class="card-h"><h3>Reports</h3><span class="grow"></span>${d.running ? `<span class="chip live"><span class="dot"></span>${d.running} running</span>` : ""}</div>
          <div class="proj-list">${d.reports.map((r) => `
            <div class="proj-item" data-id="${r.id}">
              <span class="st ${esc(r.status)}" aria-hidden="true"></span>
              <div class="grow"><a href="#" data-open="${r.id}" class="p">${esc(clip(oneLine(r.prompt), 150))}</a>
                <div class="m mono dim">#${r.id} \u00b7 ${ago(r.created_at)}${r.result_chars ? ` \u00b7 ${fmtN(Math.round(r.result_chars / 1000))}k chars` : ""}${r.children ? ` \u00b7 \u2937 ${r.children}` : ""}${r.annotations ? ` \u00b7 \u270E ${r.annotations}` : ""}
                ${r.is_home ? ' \u00b7 <span class="ok" title="This project supplies the report\'s defaults">home</span>' : ` \u00b7 <button class="linkbtn" data-home="${r.id}" title="Make this project the report's home">make home</button>`}
                ${r.also_in.map((x) => this.pill(x)).join(" ")}</div></div>
              <button class="icon-btn" data-rm="${r.id}" title="Remove from project" aria-label="Remove report ${r.id} from project">\u00d7</button>
            </div>`).join("") || '<div class="dim">No reports yet. Launch research here, or add reports from the Inbox.</div>'}</div>
          <div id="proj-similar"></div>
        </div>

        <div>
          <div class="card"><h3>Data sources</h3>
            <div class="dim" style="font-size:11.5px">New research, Ask and Lab runs in this project start with these.</div>
            <div class="proj-srcs">${d.sources.map((s) => `<span class="filechip" title="${esc(s.uri)}">${esc(s.name)} ${this.level(s.protection_level)}<button data-rmsrc="${esc(s.name)}" aria-label="Remove ${esc(s.name)}">\u00d7</button></span>`).join("") || ""}</div>
            <div id="proj-src-add"></div>
          </div>
          <div class="card"><div class="card-h"><h3>Lab runs</h3><span class="grow"></span>${d.lab_runs.length > 6 ? `<button class="linkbtn" data-a="alllab">show all ${d.lab_runs.length}</button>` : ""}</div>
            ${d.lab_runs.map((r, i) => `<div class="proj-lab${i >= 6 ? " more" : ""}" ${i >= 6 ? "hidden" : ""}><a href="#" data-open="${r.session_id}" data-lab="${r.id}">#${r.id} ${esc(clip(r.title || "untitled", 70))}</a>
              <span class="mono dim" style="font-size:10.5px">${esc(r.status)} \u00b7 <span class="${r.verdict_pass === true ? "ok" : r.verdict_pass === false ? "bad" : ""}">${esc(r.verdict)}</span></span></div>`).join("") || '<div class="dim">None yet. Open a report and use \u2697 Lab run.</div>'}
          </div>
          <div class="card"><h3>Notebooks</h3>
            ${d.notebooks.map((n) => `<div class="proj-lab"><a href="#" data-nb="${n.id}">${esc(n.title)}</a><span class="mono dim" style="font-size:10.5px">${ago(n.updated_at)} \u00b7 ${fmtN(n.chars)} chars <button class="linkbtn" data-rmnb="${n.id}">remove</button></span></div>`).join("") || '<div class="dim">Briefs built from the project land here.</div>'}
            <div id="proj-nb-add" style="margin-top:6px"></div>
          </div>
          <div class="card"><h3>Highlights</h3>
            ${d.annotations.slice(-8).reverse().map((a) => `<div class="ann-card ${esc(a.color)} note-row" data-sid="${a.session_id}" data-aid="${a.id}" tabindex="0" role="link"><div class="mono dim" style="font-size:10.5px">#${a.session_id}</div><div class="q">\u201c${esc(clip(a.quote, 220))}\u201d</div>${a.note ? `<div style="font-size:12px">${esc(a.note)}</div>` : ""}</div>`).join("") || '<div class="dim">Highlights and notes from the project\'s reports.</div>'}
          </div>
          ${d.top_citations.length ? `<div class="card"><h3>Most-cited sources</h3>${d.top_citations.map((c) => `<a class="src" href="${esc(c.url)}" target="_blank" rel="noopener noreferrer" title="${esc(c.url)}"><span>${esc(c.label)}</span>${c.sessions.length > 1 ? `<i>${c.sessions.length}</i>` : ""}</a>`).join("")}</div>` : ""}
        </div>
      </div>
    </div>`;
    const reload = () => { if (!stale(v)) this.render(v, t); };
    if (p.summary) { const el = v.querySelector("#proj-summary"); el.innerHTML = renderMd(p.summary); this.linkSessions(el); }
    v.querySelectorAll("[data-open]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); openSession(a.dataset.open); }));
    v.querySelectorAll("[data-nb]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); const n = d.notebooks.find((x) => x.id == a.dataset.nb); openNotebook(n.id, n.title); }));
    v.querySelectorAll(".note-row").forEach((el) => (el.onclick = () => { S.rtab = "notes"; S.flashAnn = +el.dataset.aid; openSession(+el.dataset.sid); }));
    v.querySelector('[data-a="settings"]').onclick = () => this.editDialog(p, reload);
    v.querySelector('[data-a="alllab"]')?.addEventListener("click", (e) => { v.querySelectorAll(".proj-lab.more").forEach((x) => (x.hidden = false)); e.target.remove(); });
    v.querySelector('[data-a="research"]').onclick = () => openLaunch({ project_id: p.id, data_sources: d.sources.map((s) => s.name) });
    v.querySelector('[data-a="add"]').onclick = () => this.addReportsDialog(p, reload);
    v.querySelector('[data-a="ws-copy"]')?.addEventListener("click", () => WSUI.copyDialog({ projects: [p.id], label: `Project: ${p.title}` }));
    v.querySelectorAll("[data-rm]").forEach((b) => (b.onclick = () => busy(b, async () => {
      await api(`/api/projects/${p.id}/items`, { method: "DELETE", body: { kind: "session", ids: [+b.dataset.rm] } });
      await Promise.all([this.load(), loadSessions()]); reload();
    })));
    v.querySelectorAll("[data-home]").forEach((b) => (b.onclick = () => busy(b, async () => {
      await api(`/api/projects/${p.id}/home`, { method: "POST", body: { session_id: +b.dataset.home } });
      delete S.cache[+b.dataset.home]; await loadSessions(); reload();
    })));
    v.querySelectorAll("[data-rmsrc]").forEach((b) => (b.onclick = () => busy(b, async () => {
      await api(`/api/projects/${p.id}/items`, { method: "DELETE", body: { kind: "source", ids: [b.dataset.rmsrc] } }); reload();
    })));
    v.querySelectorAll("[data-rmnb]").forEach((b) => (b.onclick = () => busy(b, async () => {
      await api(`/api/projects/${p.id}/items`, { method: "DELETE", body: { kind: "notebook", ids: [+b.dataset.rmnb] } }); reload();
    })));
    // add a data source: the shared picker, filtered to ones not already here
    if (typeof SRC !== "undefined") {
      SRC.load().then(() => {
        const el = v.querySelector("#proj-src-add"); if (!el) return;
        const have = new Set(d.sources.map((s) => s.name));
        const opts = SRC.list.filter((s) => !have.has(s.name));
        el.innerHTML = (opts.length ? `<select class="btn small" aria-label="Add a data source"><option value="">+ add a data source</option>${opts.map((s) => `<option value="${esc(s.name)}">${esc(s.name)} (${esc(SRC.kindLabel[s.kind] || s.kind)})</option>`).join("")}</select>` : "")
          + (typeof FB !== "undefined" ? ` <button class="btn small" id="proj-src-browse" title="Drive, Google Docs, buckets or folders on this computer">Browse files\u2026</button>` : "");
        const br = el.querySelector("#proj-src-browse");
        if (br) br.onclick = () => FB.open({ onAdded: async (s) => { await api(`/api/projects/${p.id}/items`, { method: "POST", body: { kind: "source", ids: [s.name] } }); reload(); } });
        const sel = el.querySelector("select");
        if (sel) sel.onchange = safe(async () => { if (!sel.value) return; await api(`/api/projects/${p.id}/items`, { method: "POST", body: { kind: "source", ids: [sel.value] } }); reload(); });
      }).catch(() => {});
    }
    loadNotebooks().then(() => {
      const el = v.querySelector("#proj-nb-add"); if (!el) return;
      const have = new Set(d.notebooks.map((n) => n.id));
      const opts = S.notebooks.filter((n) => !have.has(n.id));
      if (!opts.length) return;
      el.innerHTML = `<select class="btn small" aria-label="Add a notebook"><option value="">+ add a notebook</option>${opts.map((n) => `<option value="${n.id}">${esc(clip(n.title, 60))}</option>`).join("")}</select>`;
      const sel = el.querySelector("select");
      sel.onchange = safe(async () => { if (!sel.value) return; await api(`/api/projects/${p.id}/items`, { method: "POST", body: { kind: "notebook", ids: [+sel.value] } }); reload(); });
    }).catch(() => {});
    // similar unfiled reports
    api(`/api/projects/${p.id}/similar`).then((r) => {
      const el = v.querySelector("#proj-similar"); if (!el || !r.sessions.length) return;
      el.innerHTML = `<div class="section label" style="margin-top:14px">Unfiled reports that look like this project</div>${r.sessions.map((x) => `<div class="proj-sug"><a href="#" data-open="${x.id}">#${x.id} ${esc(clip(x.prompt, 110))}</a><span class="mono dim" style="font-size:10.5px">${Math.round(x.score * 100)}%</span><button class="btn small" data-addsug="${x.id}">Add</button></div>`).join("")}`;
      el.querySelectorAll("[data-open]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); openSession(a.dataset.open); }));
      el.querySelectorAll("[data-addsug]").forEach((b) => (b.onclick = () => busy(b, async () => {
        await api(`/api/projects/${p.id}/items`, { method: "POST", body: { kind: "session", ids: [+b.dataset.addsug] } });
        await Promise.all([this.load(), loadSessions()]); reload();
      })));
    }).catch(() => {});
    // summary
    const sb = v.querySelector('[data-a="summary"]');
    sb.onclick = () => busy(sb, async () => {
      sb.innerHTML = '<span class="spinner"></span> Writing';
      const r = await api(`/api/projects/${p.id}/summary`, { method: "POST" });
      toast(`Summary ready${r.cost_usd != null ? ` ($${r.cost_usd.toFixed(3)})` : ""}`, "ok"); reload();
    });
    // ask
    const ta = v.querySelector("#pa-q"), ab = v.querySelector('[data-a="ask"]');
    ta.oninput = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 160) + "px"; };
    ta.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ab.click(); } };
    ab.onclick = () => busy(ab, async () => {
      const q = ta.value.trim(); if (!q) return ta.focus();
      ab.innerHTML = '<span class="spinner"></span>';
      const out = v.querySelector("#pa-out");
      const r = await api(`/api/projects/${p.id}/ask`, { method: "POST", body: { question: q } });
      if (stale(v)) return;
      out.innerHTML = `<div class="md pa-answer">${renderMd(r.answer || "")}</div>
        <div class="mono dim" style="font-size:10.5px;margin-top:6px">searched ${r.matches.length} report${r.matches.length === 1 ? "" : "s"}${r.sources.length ? ` + ${r.sources.map(esc).join(", ")}` : ""}${r.cost_usd != null ? ` \u00b7 $${r.cost_usd.toFixed(3)}` : ""}
        \u00b7 <button class="linkbtn" data-pa="nb">save to notebook</button></div>`;
      this.linkSessions(out);
      out.querySelector('[data-pa="nb"]').onclick = safe(async () => {
        const nb = await api("/api/notebooks", { method: "POST", body: { title: `Q: ${clip(q, 80)}`, content: `## ${q}\n\n${r.answer}\n\n*Asked of project \u201c${p.title}\u201d*\n` } });
        await api(`/api/projects/${p.id}/items`, { method: "POST", body: { kind: "notebook", ids: [nb.id] } });
        toast("Saved as a notebook in this project", "ok"); reload();
      });
    });
    // export
    const ex = v.querySelector('[data-a="export"]');
    ex.onchange = safe(async (e) => {
      const f = e.target.value; e.target.value = ""; if (!f) return;
      if (f.startsWith("brief:")) return this.brief(p, f.slice(6), reload);
      if (f === "audio") return this.audio(p);
      if (f === "zip") { window.location.href = WS.q(`/api/projects/${p.id}/export?format=zip`); return toast("Building the research package\u2026"); }
      if (f === "html" || f === "print") {
        const o = await api(`/api/projects/${p.id}/export?format=md`);
        const html = standaloneHtml(p.title, `<article class="md">${renderMd(o.content)}</article>`);
        if (f === "html") return download(o.filename.replace(/\.md$/, ".html"), html, "text/html");
        const w = window.open("", "_blank"); if (!w) return toast("Allow pop-ups to print", "err");
        w.document.write(html); w.document.close(); setTimeout(() => w.print(), 400); return;
      }
      const o = await api(`/api/projects/${p.id}/export?format=${f}`);
      const types = { md: "text/markdown", json: "application/json", bib: "application/x-bibtex", csv: "text/csv" };
      download(o.filename, f === "json" ? JSON.stringify(o.content, null, 2) : o.content, types[f]);
    });
  },
  linkSessions(el) {
    // turn "Session #12" / "[Session #12]" in AI text into links
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    const hits = [];
    while (walker.nextNode()) if (/Session #?\d+/.test(walker.currentNode.nodeValue)) hits.push(walker.currentNode);
    for (const n of hits) {
      const span = document.createElement("span");
      span.innerHTML = esc(n.nodeValue).replace(/Session #?(\d+)/g, '<a href="#" class="cite-sess" data-open="$1">Session #$1</a>');
      n.replaceWith(span);
    }
    el.querySelectorAll(".cite-sess").forEach((a) => (a.onclick = (e) => { e.preventDefault(); openSession(a.dataset.open); }));
  },
  brief(p, style, after) {
    const label = (this.briefStyles.find(([k]) => k === style) || [style, style])[1];
    return confirmBox(`Build a ${label.toLowerCase()}?`, `Gemini writes it from every report in \u201c${p.title}\u201d, keeping citations, and saves it as a notebook in the project. Usually a few cents.`, "Build").then((ok) => {
      if (!ok) return;
      toast(`Building ${label.toLowerCase()}\u2026`);
      return api(`/api/projects/${p.id}/brief`, { method: "POST", body: { style } }).then((r) => {
        toast(`${label} ready${r.cost_usd != null ? ` ($${r.cost_usd.toFixed(3)})` : ""}`, "ok");
        loadNotebooks().then(() => openNotebook(r.notebook.id, r.notebook.title)); after && after();
      }).catch((e) => toast(e.message, "err"));
    });
  },
  audio(p) {
    const voices = ["Charon", "Kore", "Puck", "Aoede", "Fenrir", "Leda", "Orus", "Zephyr"];
    MODAL.open(`<h3>AI voice overview</h3><div class="dim">A 2 to 3 minute spoken briefing of the whole project${p.summary ? ", built from its AI summary" : ""}, read in the voice you pick. About $0.05\u20130.10.</div>
      <div class="field" style="margin-top:12px"><label for="pa-voice">Voice</label><select id="pa-voice">${voices.map((x) => `<option ${x === (localStorage.getItem("dr.aivoice") || "Charon") ? "selected" : ""}>${x}</option>`).join("")}</select></div>
      <div class="acts"><button class="btn" data-x="0">Cancel</button><button class="btn primary" data-x="1">Create audio</button></div>`);
    $('#modal [data-x="0"]').onclick = () => MODAL.close();
    $('#modal [data-x="1"]').onclick = async () => {
      const voice = $("#pa-voice").value; localStorage.setItem("dr.aivoice", voice); MODAL.close();
      toast("Creating the audio overview. It pops up when ready.");
      try {
        const { job } = await api(`/api/projects/${p.id}/audio`, { method: "POST", body: { voice } });
        let j;
        for (;;) { await new Promise((r) => setTimeout(r, 2500)); j = await api(`/api/audio/jobs/${job}`); if (j.status !== "running") break; }
        if (j.status === "error") throw new Error(j.error);
        AUDIO.player(j.result, p.title); NOTIFY.send("Audio ready", p.title);
      } catch (e) { toast(`Audio failed: ${e.message}`, "err"); }
    };
  },
  addReportsDialog(p, after) {
    const inProj = new Set();
    const rows = S.sessions.filter((s) => !s.parent_id && !(s.projects || []).some((x) => x.id === p.id));
    MODAL.open(`<h3>Add reports to \u201c${esc(p.title)}\u201d</h3>
      <input class="inline-input" id="pa-filter" placeholder="Filter" aria-label="Filter reports" style="margin:8px 0">
      <label class="dim" style="font-size:11.5px"><input type="checkbox" id="pa-inbox" checked> Inbox only (not in any project)</label>
      <div class="pj-file" id="pa-list"></div>
      <div class="acts"><span class="dim" id="pa-n" style="margin-right:auto"></span><button class="btn" data-x="0">Cancel</button><button class="btn primary" data-x="1">Add</button></div>`, { cls: "wide" });
    const draw = () => {
      const q = $("#pa-filter").value.trim().toLowerCase(), inbox = $("#pa-inbox").checked;
      const shown = rows.filter((s) => (!inbox || !(s.projects || []).length) && (!q || s.prompt.toLowerCase().includes(q))).slice(0, 300);
      $("#pa-list").innerHTML = shown.map((s) => `<label class="pj-file-row"><input type="checkbox" value="${s.id}" ${inProj.has(s.id) ? "checked" : ""}><span class="mono dim">#${s.id}</span> <span class="t">${esc(clip(oneLine(s.prompt), 120))}</span>${(s.projects || []).map((x) => this.pill(x)).join("")}</label>`).join("") || '<div class="dim">Nothing matches.</div>';
      $$("#pa-list input").forEach((c) => (c.onchange = () => { c.checked ? inProj.add(+c.value) : inProj.delete(+c.value); $("#pa-n").textContent = `${inProj.size} selected`; }));
    };
    $("#pa-filter").oninput = debounce(draw, 120); $("#pa-inbox").onchange = draw; draw();
    $('#modal [data-x="0"]').onclick = () => MODAL.close();
    const go = $('#modal [data-x="1"]');
    go.onclick = () => busy(go, async () => {
      if (!inProj.size) return toast("Tick at least one report", "err");
      await api(`/api/projects/${p.id}/items`, { method: "POST", body: { kind: "session", ids: [...inProj] } });
      MODAL.close(); await Promise.all([this.load(), loadSessions()]); toast(`Added ${inProj.size} report${inProj.size > 1 ? "s" : ""}`, "ok"); after && after();
    });
  },

  // ------------------------------------------------------------------ sorter
  async renderSorter(v) {
    v.innerHTML = `<div class="pad"><div class="runs-head"><h2>Sort the inbox</h2><span class="grow"></span><span class="dim" id="so-n"></span><button class="btn small" id="so-ai" title="One Gemini Flash call names every group (under a cent)" hidden>Name groups with AI</button></div>
      <p class="dim" style="max-width:760px">Suggested groups of reports that are not in a project yet: first from tags you applied, then from reports whose content is similar. Nothing is filed until you accept a group; untick anything that does not belong.</p>
      <div id="so-body"><span class="spinner"></span></div></div>`;
    let r;
    try { r = await api("/api/projects/suggestions"); } catch (e) { if (!stale(v)) v.querySelector("#so-body").innerHTML = `<div class="err-banner">${esc(e.message)}</div>`; return; }
    if (stale(v)) return;
    v.querySelector("#so-n").textContent = `${r.inbox} in the inbox \u00b7 ${r.embedded} indexed for similarity`;
    const body = v.querySelector("#so-body");
    if (!r.groups.length) { body.innerHTML = `<div class="empty-result">No groups to suggest. ${r.embedded < r.inbox ? "Run a Semantic search once to index older reports, then come back." : "File reports by hand from each report's Project button."}</div>`; return; }
    body.innerHTML = r.groups.map((g, i) => `
      <div class="card so-group" data-g="${i}">
        <div class="card-h"><input class="inline-input so-name" value="${esc(g.label)}" aria-label="Project name" style="width:260px;flex:none">
          <span class="chip">${esc(g.source)}</span>${g.cohesion != null ? `<span class="mono dim" style="font-size:10.5px">similarity ${Math.round(g.cohesion * 100)}%</span>` : ""}
          <span class="grow"></span>
          <select class="btn small so-target" aria-label="Into"><option value="">new project</option>${this.list.map((p) => `<option value="${p.id}" ${g.existing_project === p.id ? "selected" : ""}>into: ${esc(clip(p.title, 40))}</option>`).join("")}</select>
          <button class="btn small primary" data-accept="${i}">Accept</button><button class="btn small" data-skip="${i}">Skip</button></div>
        ${g.sessions.map((s) => `<label class="pj-file-row"><input type="checkbox" value="${s}" checked><span class="mono dim">#${s}</span> <span class="t">${esc(g.prompts[s] || "")}</span></label>`).join("")}
      </div>`).join("");
    body.querySelectorAll("[data-skip]").forEach((b) => (b.onclick = () => b.closest(".so-group").remove()));
    const aiBtn = v.querySelector("#so-ai"); aiBtn.hidden = false;
    aiBtn.onclick = () => busy(aiBtn, async () => {
      const cards = $$(".so-group", body);
      const groups = cards.map((c) => ({ prompts: $$(".pj-file-row .t", c).map((x) => x.textContent) }));
      const r2 = await api("/api/projects/suggestions/name", { method: "POST", body: { groups } });
      cards.forEach((c, i) => { if (r2.names[i]) c.querySelector(".so-name").value = r2.names[i]; });
      toast(`Named ${r2.names.length} groups${r2.cost_usd != null ? ` ($${r2.cost_usd.toFixed(4)})` : ""}`, "ok");
    });
    body.querySelectorAll("[data-accept]").forEach((b) => (b.onclick = () => busy(b, async () => {
      const card = b.closest(".so-group");
      const sessions = $$('input[type="checkbox"]:checked', card).map((c) => +c.value);
      const target = card.querySelector(".so-target").value;
      const p = await api("/api/projects/suggestions/accept", { method: "POST", body: { sessions, title: card.querySelector(".so-name").value.trim(), project_id: target ? +target : null } });
      card.remove(); await Promise.all([this.load(), loadSessions()]);
      toast(`${sessions.length} report${sessions.length > 1 ? "s" : ""} filed in \u201c${p.title}\u201d`, "ok");
      v.querySelector("#so-n").textContent = `${this.inbox} in the inbox`;
    })));
  },
};
