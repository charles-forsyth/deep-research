/* Action registry (v0.54.0, U1). Every action a page offers is listed here once: its
   id, label, the page it belongs to, and its weight. Toolbars, "..." menus and the
   command palette all draw from this table, so moving an action into a menu never
   drops it. tests/dashboard/test_ui_inventory.py checks that every control the old
   interface had still has a home here or in the page shell.

   weight: "primary" = a button on the page; "menu" = in the "..." menu;
           "share" = in the Share menu; "danger" = last in the "..." menu, in red.
   The page that renders the actions supplies the handlers and an available() check. */
"use strict";

const ACTIONS = [
  // a report (scope "report")
  { id: "ask", scope: "report", label: "Ask a follow-up", weight: "primary", key: "/" },
  { id: "listen", scope: "report", label: "Listen", weight: "primary", hint: "Read the report aloud, word for word (free, browser voice)" },
  { id: "stop", scope: "report", label: "Stop research", weight: "primary" },
  { id: "rerun", scope: "report", label: "Re-run", weight: "menu", hint: "Run this question again and compare" },
  { id: "info", scope: "report", label: "Info", weight: "primary", key: "i", hint: "Details, notes, outline and live log" },
  { id: "star", scope: "report", label: "Star", weight: "toggle" },
  { id: "copy", scope: "report", label: "Copy report", weight: "share" },
  { id: "to-nb", scope: "report", label: "Send to notebook", weight: "share" },
  { id: "export-md", scope: "report", label: "Markdown (with notes)", weight: "share" },
  { id: "export-md-rec", scope: "report", label: "Markdown, with sub-reports", weight: "share" },
  { id: "export-html", scope: "report", label: "Standalone HTML", weight: "share" },
  { id: "export-json", scope: "report", label: "JSON", weight: "share" },
  { id: "print", scope: "report", label: "Print or PDF", weight: "share" },
  { id: "audio-full", scope: "report", label: "Audio: full report (AI voice)", weight: "share" },
  { id: "audio-summary", scope: "report", label: "Audio: spoken summary (AI voice)", weight: "share" },
  { id: "brief", scope: "report", label: "Brief: executive, slides, email, grant, lay, lit review", weight: "share" },
  { id: "find", scope: "report", label: "Find in report", weight: "menu", key: "Ctrl F" },
  { id: "project", scope: "report", label: "Projects\u2026", weight: "menu", hint: "File this report in projects" },
  { id: "lab", scope: "report", label: "Lab run on this report\u2026", weight: "menu" },
  { id: "tree", scope: "report", label: "Sub-report tree", weight: "menu" },
  { id: "compare", scope: "report", label: "Compare with re-run", weight: "menu" },
  { id: "ws-copy", scope: "report", label: "Copy to another workspace\u2026", weight: "menu" },
  { id: "delete", scope: "report", label: "Delete report\u2026", weight: "danger" },

  // pages (scope "app"): sidebar, Home and the palette
  { id: "new", scope: "app", label: "New research", key: "N" },
  { id: "home", scope: "app", label: "Home" },
  { id: "projects", scope: "app", label: "Projects" },
  { id: "new-project", scope: "app", label: "New project" },
  { id: "sort-inbox", scope: "app", label: "Sort inbox into projects" },
  { id: "labruns", scope: "app", label: "Lab runs" },
  { id: "notes", scope: "app", label: "All notes and highlights" },
  { id: "sources", scope: "app", label: "Data sources" },
  { id: "search", scope: "app", label: "Semantic search" },
  { id: "notebook", scope: "app", label: "New notebook" },
  { id: "map", scope: "app", label: "Research map" },
  { id: "settings", scope: "app", label: "Settings" },
  { id: "stats", scope: "app", label: "Library stats" },
  { id: "workspaces", scope: "app", label: "Workspaces" },
  { id: "refresh", scope: "app", label: "Refresh reports" },
];

const ACT = {
  byId: Object.fromEntries(ACTIONS.map((a) => [a.id, a])),
  ctx: null, // {scope, handlers, available} of the page on screen, for the palette

  of(scope, weight) { return ACTIONS.filter((a) => a.scope === scope && (!weight || a.weight === weight)); },

  // the page on screen registers its handlers; the palette lists what is available
  bind(scope, handlers, available = () => true) { this.ctx = { scope, handlers, available }; },
  unbind() { this.ctx = null; },
  run(id) {
    const c = this.ctx;
    if (c && c.handlers[id] && c.available(id)) return c.handlers[id]();
    const h = APP_ACTIONS[id];
    if (h) return h();
  },
  // [group, label, fn] rows for the command palette
  paletteRows() {
    const rows = [];
    const c = this.ctx;
    if (c) for (const a of this.of(c.scope)) if (c.handlers[a.id] && c.available(a.id)) rows.push(["report", a.label, () => c.handlers[a.id]()]);
    for (const a of this.of("app")) if (APP_ACTIONS[a.id]) rows.push(["go", a.label, APP_ACTIONS[a.id]]);
    return rows;
  },

  // a small popup menu anchored to a button; items = [{id,label,danger,sep,hint}]
  menu(anchor, items, onPick) {
    this.closeMenu();
    const m = document.createElement("div");
    m.className = "popmenu"; m.setAttribute("role", "menu");
    m.innerHTML = items.map((it, i) => it.sep ? `<div class="pm-sep" role="separator"></div>`
      : it.head ? `<div class="pm-head">${esc(it.head)}</div>`
      : `<button role="menuitem" data-i="${i}" class="${it.danger ? "danger" : ""}${it.on ? " on" : ""}" ${it.hint ? `title="${esc(it.hint)}"` : ""}>${esc(it.label)}${it.key ? `<span class="kbd">${esc(it.key)}</span>` : ""}</button>`).join("");
    document.body.appendChild(m);
    const r = anchor.getBoundingClientRect();
    const w = Math.min(300, window.innerWidth - 16);
    m.style.minWidth = Math.min(220, w) + "px"; m.style.maxWidth = w + "px";
    const left = Math.max(8, Math.min(r.right - m.offsetWidth, window.innerWidth - m.offsetWidth - 8));
    let top = r.bottom + 4;
    if (top + m.offsetHeight > window.innerHeight - 8) top = Math.max(8, r.top - m.offsetHeight - 4);
    m.style.left = left + "px"; m.style.top = top + "px";
    m.onclick = (e) => { const b = e.target.closest("[data-i]"); if (!b) return; this.closeMenu(); onPick(items[+b.dataset.i]); };
    m.onkeydown = (e) => {
      const bs = [...m.querySelectorAll("button")]; const i = bs.indexOf(document.activeElement);
      if (e.key === "ArrowDown") { e.preventDefault(); bs[(i + 1) % bs.length].focus(); }
      else if (e.key === "ArrowUp") { e.preventDefault(); bs[(i - 1 + bs.length) % bs.length].focus(); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); this.closeMenu(); anchor.focus(); }
      else if (e.key === "Tab") this.closeMenu();
    };
    this.open = { m, anchor };
    anchor.setAttribute("aria-expanded", "true");
    m.querySelector("button")?.focus();
  },
  closeMenu() {
    if (!this.open) return;
    this.open.anchor.setAttribute("aria-expanded", "false");
    this.open.m.remove(); this.open = null;
  },
};
document.addEventListener("mousedown", (e) => { if (ACT.open && !ACT.open.m.contains(e.target) && e.target !== ACT.open.anchor) ACT.closeMenu(); });
window.addEventListener("resize", () => ACT.closeMenu());
// handlers for app-wide actions; filled in by app.js once its functions exist
const APP_ACTIONS = {};
