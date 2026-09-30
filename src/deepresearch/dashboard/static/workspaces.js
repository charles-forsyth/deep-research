/* Workspace switcher (v0.41): a small menu beside the brand in the top bar.
   Switching stores the choice (WS in app.js) and reloads the page, so nothing from the
   previous workspace can linger on screen. Loaded after app.js helpers exist. */
"use strict";

const WSUI = {
  list: [],
  enabled: false,

  async init() {
    let d;
    try { d = await api("/api/workspaces"); } catch { return; }
    this.enabled = !!d.enabled;
    if (!this.enabled) return;
    this.list = d.workspaces || [];
    // a workspace deleted or archived elsewhere: fall back to Main
    const cur = this.list.find((w) => w.slug === WS.get() && !w.archived);
    if (!cur && WS.get() !== "main") { WS.set("main"); location.reload(); return; }
    this.paint(cur || this.list.find((w) => w.slug === "main"));
  },

  paint(cur) {
    const host = $("#ws-switch");
    if (!host || !cur) return;
    host.hidden = false;
    host.dataset.ws = cur.slug;
    host.dataset.color = cur.is_main ? "" : (cur.color || "teal");
    host.querySelector(".ws-name").textContent = cur.name;
    host.title = cur.is_main ? "Workspace: Main (your library). Click to switch." : `Workspace: ${cur.name}. Click to switch.`;
    // subtle cue outside Main: a thin tinted line under the top bar (no banners)
    document.body.dataset.ws = cur.is_main ? "" : (cur.color || "teal");
    document.title = cur.is_main ? "Deep Research // Workstation" : `${cur.name} // Deep Research`;
    host.onclick = (e) => { e.stopPropagation(); this.menu(); };
  },

  menu() {
    const cur = WS.get();
    const live = this.list.filter((w) => !w.archived);
    const archived = this.list.filter((w) => w.archived);
    const row = (w) => `<button class="ws-row${w.slug === cur ? " on" : ""}" data-ws="${esc(w.slug)}" role="menuitemradio" aria-checked="${w.slug === cur}">
        <span class="ws-dot" data-color="${w.is_main ? "" : esc(w.color || "teal")}"></span>
        <span class="ws-row-name">${esc(w.name)}</span>
        <span class="dim ws-row-meta">${w.reports} reports \u00b7 ${w.projects} projects${w.active_lab_runs ? ` \u00b7 ${w.active_lab_runs} Lab running` : ""}</span>
      </button>`;
    MODAL.open(`
      <div class="modal-head"><h3>Workspaces</h3><button class="icon-btn modal-x" data-x="close" aria-label="Close">\u2715</button></div>
      <p class="dim" style="font-size:12px;margin:0 0 10px">Each workspace is a separate library of reports, projects, notes, Lab runs and data sources. <b>Main</b> is your original library.</p>
      <div class="ws-list" role="menu">${live.map(row).join("")}</div>
      ${archived.length ? `<details class="ws-arch"><summary class="dim">Archived (${archived.length})</summary>${archived.map((w) => `<div class="ws-row arch"><span class="ws-row-name">${esc(w.name)}</span><button class="btn small" data-unarch="${esc(w.slug)}">Unarchive</button></div>`).join("")}</details>` : ""}
      <div class="ws-acts">
        <button class="btn small primary" data-x="new">+ New workspace</button>
        <button class="btn small" data-x="dup" title="Copy this whole workspace">Duplicate current</button>
        ${cur !== "main" ? `<button class="btn small" data-x="rename">Rename</button><button class="btn small" data-x="archive">Archive</button><button class="btn small danger" data-x="delete">Delete\u2026</button>` : ""}
      </div>`, { label: "Workspaces", cls: "ws-modal" });
    const m = $("#modal");
    m.querySelectorAll(".ws-row[data-ws]").forEach((b) => (b.onclick = () => this.switchTo(b.dataset.ws)));
    m.querySelectorAll("[data-unarch]").forEach((b) => (b.onclick = () => busy(b, async () => {
      await api(`/api/workspaces/${b.dataset.unarch}`, { method: "PATCH", body: { archived: false } });
      await this.reloadList(); this.menu();
    })));
    m.querySelector('[data-x="close"]').onclick = () => MODAL.close();
    m.querySelector('[data-x="new"]').onclick = () => this.create();
    m.querySelector('[data-x="dup"]').onclick = () => this.duplicate();
    m.querySelector('[data-x="rename"]')?.addEventListener("click", () => this.rename());
    m.querySelector('[data-x="archive"]')?.addEventListener("click", () => this.archive());
    m.querySelector('[data-x="delete"]')?.addEventListener("click", () => this.remove());
  },

  async reloadList() {
    const d = await api("/api/workspaces");
    this.list = d.workspaces || [];
  },

  switchTo(slug) {
    if (slug === WS.get()) { MODAL.close(); return; }
    if (typeof NB !== "undefined" && NB.dirty) NB.saveNow();
    WS.set(slug);
    location.reload();
  },

  form(title, fields, submit) {
    MODAL.open(`
      <div class="modal-head"><h3>${esc(title)}</h3><button class="icon-btn modal-x" data-x="close" aria-label="Close">\u2715</button></div>
      ${fields}
      <div class="acts" style="margin-top:12px"><button class="btn" data-x="close">Cancel</button><button class="btn primary" data-x="ok">${esc(submit)}</button></div>`, { label: title });
    const m = $("#modal");
    m.querySelectorAll('[data-x="close"]').forEach((b) => (b.onclick = () => this.menu()));
    return m;
  },

  create() {
    const m = this.form("New workspace", `
      <div class="field"><label for="ws-name">Name</label><input id="ws-name" autofocus placeholder="e.g. Demo" maxlength="80"></div>
      <div class="field"><label for="ws-desc">Description <span class="dim">(optional)</span></label><input id="ws-desc" maxlength="500"></div>
      <p class="dim" style="font-size:11.5px">Starts empty. Your Gemini key, cluster settings and the Lab's lessons are shared; reports, projects, notes, Lab runs and data sources are not.</p>`, "Create and open");
    const ok = m.querySelector('[data-x="ok"]');
    const go = () => busy(ok, async () => {
      const name = $("#ws-name").value.trim();
      if (!name) { toast("Give it a name", "err"); return; }
      const w = await api("/api/workspaces", { method: "POST", body: { name, description: $("#ws-desc").value.trim() } });
      toast(`Created ${w.name}`, "ok");
      this.switchTo(w.slug);
    });
    ok.onclick = go;
    $("#ws-name").onkeydown = (e) => { if (e.key === "Enter") go(); };
  },

  duplicate() {
    const cur = this.list.find((w) => w.slug === WS.get()) || { name: "Main" };
    const m = this.form(`Duplicate ${cur.name}`, `
      <div class="field"><label for="ws-name">Name of the copy</label><input id="ws-name" autofocus value="${esc(cur.name)} copy" maxlength="80"></div>
      <p class="dim" style="font-size:11.5px">Copies everything (reports, projects, notes, Lab runs and their outputs, audio). ${esc(cur.name)} itself is only read.</p>`, "Duplicate and open");
    const ok = m.querySelector('[data-x="ok"]');
    ok.onclick = () => busy(ok, async () => {
      const w = await api(`/api/workspaces/${WS.get()}/duplicate`, { method: "POST", body: { name: $("#ws-name").value.trim() } });
      toast(`Copied to ${w.name}`, "ok");
      this.switchTo(w.slug);
    });
  },

  rename() {
    const cur = this.list.find((w) => w.slug === WS.get());
    if (!cur) return;
    const m = this.form("Rename workspace", `<div class="field"><label for="ws-name">Name</label><input id="ws-name" autofocus value="${esc(cur.name)}" maxlength="80"></div>`, "Save");
    const ok = m.querySelector('[data-x="ok"]');
    ok.onclick = () => busy(ok, async () => {
      await api(`/api/workspaces/${cur.slug}`, { method: "PATCH", body: { name: $("#ws-name").value.trim() } });
      location.reload();
    });
  },

  archive() {
    const cur = this.list.find((w) => w.slug === WS.get());
    if (!cur) return;
    const m = this.form(`Archive ${cur.name}?`, `<p style="font-size:12.5px">It disappears from the list (nothing is deleted) and you go back to Main. Unarchive it from this menu any time.</p>`, "Archive");
    const ok = m.querySelector('[data-x="ok"]');
    ok.onclick = () => busy(ok, async () => {
      await api(`/api/workspaces/${cur.slug}`, { method: "PATCH", body: { archived: true } });
      this.switchTo("main");
    });
  },

  remove() {
    const cur = this.list.find((w) => w.slug === WS.get());
    if (!cur || cur.is_main) return;
    const m = this.form(`Delete ${cur.name}?`, `
      <p style="font-size:12.5px">The workspace folder moves to the trash folder (<span class="mono">workspaces/.trash/</span>) and can be restored by hand; nothing is erased. Main is not affected.</p>
      <div class="field"><label for="ws-confirm">Type <b class="mono">${esc(cur.slug)}</b> to confirm</label><input id="ws-confirm" autocomplete="off"></div>`, "Delete");
    const ok = m.querySelector('[data-x="ok"]');
    ok.classList.add("danger");
    ok.onclick = () => busy(ok, async () => {
      const v = $("#ws-confirm").value.trim();
      if (v !== cur.slug) { toast("The id does not match", "err"); return; }
      await api(`/api/workspaces/${cur.slug}`, { method: "DELETE", body: { confirm: v } });
      toast(`${cur.name} moved to the trash`, "ok");
      this.switchTo("main");
    });
  },
};
