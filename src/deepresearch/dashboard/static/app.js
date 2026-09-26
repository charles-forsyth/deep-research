/* Deep Research // analyst workstation. Vanilla JS, no build step. */
"use strict";

// ---------------------------------------------------------------- utilities
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtN = (n) => Number(n || 0).toLocaleString();
const clip = (s, n) => (s = String(s || "").replace(/\s+/g, " ").trim()).length > n ? s.slice(0, n - 1) + "\u2026" : s;
const ago = (iso) => {
  if (!iso) return "";
  const d = (Date.now() - new Date(iso).getTime()) / 1000;
  if (d < 60) return "just now";
  if (d < 3600) return Math.floor(d / 60) + "m ago";
  if (d < 86400) return Math.floor(d / 3600) + "h ago";
  if (d < 86400 * 7) return Math.floor(d / 86400) + "d ago";
  const dt = new Date(iso);
  const opts = { month: "short", day: "numeric" };
  if (dt.getFullYear() !== new Date().getFullYear()) opts.year = "numeric";
  return dt.toLocaleDateString(undefined, opts);
};
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, init);
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) throw new Error((data && data.error) || `${res.status} ${res.statusText}`);
  return data;
}

function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "err" ? 7000 : 3200);
}
function status(msg) { $("#sb-left").textContent = msg; }

marked.setOptions({ gfm: true, breaks: false });
function renderMd(md) {
  const html = DOMPurify.sanitize(marked.parse(md || ""), { ADD_ATTR: ["target"] });
  return html;
}
function hostOf(u) { try { return new URL(u).hostname.replace(/^www\./, ""); } catch { return u; } }
// Grounding links are redirects (vertexaisearch.cloud.google.com/...); the real
// source is the markdown link text. Group by that label.
function extractSources(md) {
  const seen = new Map();
  const add = (label, url) => {
    const redirect = /vertexaisearch\.cloud\.google\.com|grounding-api-redirect/.test(url);
    const key = (redirect ? label : hostOf(url)).trim().toLowerCase() || url;
    const cur = seen.get(key);
    if (cur) cur.n++;
    else seen.set(key, { label: redirect ? label.trim() : hostOf(url), url, n: 1 });
  };
  const linkRe = /\[([^\]]{1,200})\]\((https?:\/\/[^\s)]+)\)/g;
  let m; const covered = new Set();
  while ((m = linkRe.exec(md))) { add(m[1], m[2]); covered.add(m[2]); }
  for (const u of md.match(/https?:\/\/[^\s)\]>"']+/g) || []) if (!covered.has(u)) add(hostOf(u), u);
  return [...seen.values()].sort((a, b) => b.n - a.n || a.label.localeCompare(b.label));
}
function niceTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso); if (isNaN(d)) return iso;
  return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}
function slug(s) { return String(s).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60); }

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch {
    // Non-secure origin (http://LAN-IP): fall back to execCommand.
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove();
  }
  toast("Copied to clipboard", "ok");
}
function download(name, content, type = "text/markdown") {
  const blob = new Blob([content], { type: type + ";charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = name;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
function standaloneHtml(title, bodyHtml) {
  const css = [...document.styleSheets].map((s) => { try { return [...s.cssRules].map((r) => r.cssText).join("\n"); } catch { return ""; } }).join("\n");
  return `<!doctype html><html><head><meta charset="utf-8"><title>${esc(title)}</title><style>${css}
body{overflow:auto}body::before{display:none}</style></head><body><div class="reader">${bodyHtml}</div></body></html>`;
}

// ---------------------------------------------------------------- state
const S = {
  sessions: [],
  filter: "all",
  q: "",
  rootsOnly: true,
  tabs: [],          // {key, kind, id?, title}
  active: null,
  cache: {},         // session id -> detail
  notebooks: [],
  activeNotebook: null,
  rtab: "info",
  health: null,
  stats: null,
};
const LS_TABS = "dr.tabs.v1";

// ---------------------------------------------------------------- data loads
async function loadSessions() {
  const qs = S.q ? `?q=${encodeURIComponent(S.q)}` : "";
  const data = await api(`/api/sessions${qs}`);
  NOTIFY.diff(S.sessions, data.sessions);
  S.sessions = data.sessions;
  renderSessionList();
  renderTelemetry();
}
async function loadStats() {
  S.stats = await api("/api/stats");
  renderTelemetry();
}
async function loadSession(id, force = false) {
  if (!force && S.cache[id]) return S.cache[id];
  const d = await api(`/api/sessions/${id}`);
  S.cache[id] = d;
  return d;
}
async function loadNotebooks() {
  S.notebooks = (await api("/api/notebooks")).notebooks;
}

// ---------------------------------------------------------------- telemetry
function renderTelemetry() {
  const st = S.stats?.by_status || {};
  const live = S.sessions.filter((s) => s.status === "running").length;
  const key = S.health?.api_key;
  $("#telemetry").innerHTML = `
    <span class="chip ${live ? "live" : ""}"><span class="dot"></span>LIVE <b>${live}</b></span>
    <span class="chip ok"><span class="dot"></span>COMPLETE <b>${fmtN(st.completed)}</b></span>
    <span class="chip ${(st.failed || 0) + (st.crashed || 0) ? "bad" : ""}"><span class="dot"></span>FAILED <b>${fmtN((st.failed || 0) + (st.crashed || 0))}</b></span>
    <span class="chip"><span class="dot"></span>CORPUS <b>${fmtN(Math.round((S.stats?.result_chars || 0) / 1000))}k</b> chars</span>
    <span class="chip ${key ? "ok" : "bad"}"><span class="dot"></span>API KEY <b>${key ? "OK" : "MISSING"}</b></span>`;
  $("#sb-right").textContent = `${fmtN(S.stats?.total)} sessions \u00b7 ${fmtN(S.stats?.notebooks)} notebooks \u00b7 ${fmtN(S.stats?.annotations)} annotations`;
}

// ---------------------------------------------------------------- archive (left)
function filteredSessions() {
  return S.sessions.filter((s) => {
    if (S.rootsOnly && s.parent_id && !S.q) return false;
    if (S.filter === "running") return s.status === "running";
    if (S.filter === "starred") return s.starred;
    if (S.filter === "failed") return ["failed", "crashed", "cancelled"].includes(s.status);
    return true;
  });
}
function renderSessionList() {
  const list = filteredSessions();
  $("#count").textContent = `${list.length} shown`;
  const activeId = currentTab()?.kind === "session" ? currentTab().id : null;
  $("#session-list").innerHTML = list.map((s) => `
    <div class="sess ${s.id === activeId ? "active" : ""}" data-id="${s.id}">
      <span class="st ${esc(s.status)}"></span>
      <div>
        <div class="p">${esc(clip(s.prompt, 160))}</div>
        <div class="m">
          <span>${ago(s.created_at)}</span>
          ${s.result_chars >= 1000 ? `<span>${fmtN(Math.round(s.result_chars / 1000))}k chars</span>` : ""}
          ${s.children ? `<span>\u2937 ${s.children}</span>` : ""}
          ${s.annotations ? `<span>\u270E ${s.annotations}</span>` : ""}
          ${s.tags.map((t) => `<span class="tagpill">${esc(t)}</span>`).join("")}
        </div>
      </div>
      <div style="text-align:right">
        <div class="id">#${s.id}</div>
        ${s.starred ? '<div class="star">\u2605</div>' : ""}
      </div>
    </div>`).join("") || `<div class="dim" style="padding:20px;text-align:center">No sessions match.</div>`;
}

// ---------------------------------------------------------------- tabs
function currentTab() { return S.tabs.find((t) => t.key === S.active); }
function saveTabs() {
  localStorage.setItem(LS_TABS, JSON.stringify({ tabs: S.tabs.filter((t) => t.kind !== "launch"), active: S.active }));
}
function openTab(tab) {
  if (!S.tabs.find((t) => t.key === tab.key)) S.tabs.push(tab);
  S.active = tab.key;
  saveTabs();
  renderTabs();
  renderStage();
}
function closeTab(key) {
  const i = S.tabs.findIndex((t) => t.key === key);
  if (i < 0) return;
  if (S.tabs[i].kind === "notebook" && NB.dirty) NB.saveNow();
  S.tabs.splice(i, 1);
  if (S.active === key) S.active = (S.tabs[i] || S.tabs[i - 1] || S.tabs[0])?.key || null;
  if (!S.tabs.length) { S.tabs.push({ key: "home", kind: "home", title: "Mission control" }); S.active = "home"; }
  saveTabs(); renderTabs(); renderStage();
}
const ICONS = { home: "\u25CE", session: "\u00a7", notebook: "\u270E", launch: "+", search: "\u2315", tree: "\u2937", map: "\u2B21", compare: "\u21C4" };
function renderTabs() {
  $("#tabs").innerHTML = S.tabs.map((t) => `
    <div class="tab ${t.key === S.active ? "on" : ""}" data-key="${esc(t.key)}" title="${esc(t.title)}">
      <span class="ico">${ICONS[t.kind] || "\u2022"}</span>
      <span class="t">${esc(clip(t.title, 34))}</span>
      ${t.kind !== "home" ? `<button class="x" data-close="${esc(t.key)}" title="Close">\u00d7</button>` : ""}
    </div>`).join("");
  renderSessionList();
}
function openSession(id) { openTab({ key: `s${id}`, kind: "session", id: Number(id), title: `#${id}` }); }
function openNotebook(id, title) { openTab({ key: `n${id}`, kind: "notebook", id: Number(id), title: title || "Notebook" }); }
function openLaunch(prefill) { S.launchPrefill = prefill || null; openTab({ key: "launch", kind: "launch", title: "New research" }); }
function openSearch() { openTab({ key: "search", kind: "search", title: "Semantic search" }); }
function openTree(id) { openTab({ key: `t${id}`, kind: "tree", id: Number(id), title: `Tree #${id}` }); }
function openMap() { openTab({ key: "map", kind: "map", title: "Research map" }); }
function openCompare(a, b) { openTab({ key: `c${a}-${b}`, kind: "compare", a: Number(a), b: Number(b), title: `#${a} \u21C4 #${b}` }); }

function renderStage() {
  const t = currentTab();
  const stage = $("#stage");
  stage.innerHTML = "";
  hideSelbar();
  if (!t) return;
  READER.stop();
  document.querySelectorAll(".cite-card").forEach((c) => (c.hidden = true));
  const v = document.createElement("div");
  v.className = "view";
  stage.appendChild(v);
  const renderers = { home: renderHome, session: renderSession, notebook: renderNotebook, launch: renderLaunch, search: renderSearch, tree: renderTree, map: renderMap, compare: renderCompare };
  (renderers[t.kind] || renderHome)(v, t);
  renderRight();
}

// ---------------------------------------------------------------- home
function renderHome(v) {
  const st = S.stats || { by_status: {} };
  const recent = S.sessions.filter((s) => !s.parent_id).slice(0, 12);
  v.innerHTML = `
  <div class="pad">
    <div class="hero">
      <div>
        <h2>Research, <span>instrumented.</span></h2>
        <p>Launch autonomous deep research, watch it think in real time, then read, annotate and
        assemble findings into notebooks you can export.</p>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn" data-go="launch">+ Launch</button>
          <button class="btn" data-go="search">\u2315 Search past research</button>
          <button class="btn" data-go="notebook">\u270E New notebook</button>
          <button class="btn" data-go="map">\u2B21 Research map</button>
        </div>
      </div>
      <div class="card">
        <h3>Corpus &amp; workspace</h3>
        <div class="stat-grid">
          <div class="stat"><div class="v">${fmtN(st.total)}</div><div class="k">sessions</div></div>
          <div class="stat"><div class="v">${fmtN(st.by_status?.completed)}</div><div class="k">complete</div></div>
          <div class="stat"><div class="v">${fmtN(st.by_status?.running)}</div><div class="k">live</div></div>
          <div class="stat"><div class="v">${fmtN(Math.round((st.result_chars || 0) / 1e6 * 10) / 10)}M</div><div class="k">chars</div></div>
          <div class="stat"><div class="v">${fmtN(st.notebooks)}</div><div class="k">notebooks</div></div>
          <div class="stat"><div class="v">${fmtN(st.annotations)}</div><div class="k">annotations</div></div>
        </div>
      </div>
    </div>
    <div class="card" style="margin-bottom:18px">
      <h3>Notebooks</h3>
      <div class="recent" id="nb-cards"></div>
    </div>
    <div class="card">
      <h3>Recent research</h3>
      <div class="recent">
        ${recent.map((s) => `
          <div class="rcard" data-id="${s.id}">
            <div style="display:flex;justify-content:space-between;align-items:center">
              <span class="status-badge ${esc(s.status)}">${esc(s.status)}</span>
              <span class="id mono dim">#${s.id} \u00b7 ${ago(s.created_at)}</span>
            </div>
            <div class="p">${esc(clip(s.prompt, 220))}</div>
            <div class="mono dim" style="font-size:10.5px">${fmtN(Math.round(s.result_chars / 1000))}k chars${s.children ? ` \u00b7 ${s.children} sub-tasks` : ""}</div>
          </div>`).join("")}
      </div>
    </div>
  </div>`;
  v.querySelector('[data-go="launch"]').onclick = () => openLaunch();
  v.querySelector('[data-go="search"]').onclick = openSearch;
  v.querySelector('[data-go="notebook"]').onclick = () => NB.create();
  v.querySelector('[data-go="map"]').onclick = openMap;
  v.querySelectorAll(".rcard[data-id]").forEach((c) => (c.onclick = () => openSession(c.dataset.id)));
  loadNotebooks().then(() => {
    const el = v.querySelector("#nb-cards");
    if (!el) return;
    el.innerHTML = S.notebooks.slice(0, 8).map((n) => `
      <div class="rcard" data-nb="${n.id}"><div class="mono dim" style="font-size:10.5px">${ago(n.updated_at)} \u00b7 ${fmtN(n.chars)} chars</div>
      <div class="p" style="font-weight:600">${esc(n.title)}</div></div>`).join("")
      || `<div class="dim">No notebooks yet. Select text in any report and send it to a notebook.</div>`;
    el.querySelectorAll("[data-nb]").forEach((c) => (c.onclick = () => {
      const n = S.notebooks.find((x) => x.id == c.dataset.nb); openNotebook(n.id, n.title);
    }));
  });
}

// ---------------------------------------------------------------- session reader
async function renderSession(v, t) {
  v.innerHTML = `<div class="reader"><div class="empty-result scan">Loading session #${t.id}\u2026</div></div>`;
  let s;
  try { s = await loadSession(t.id, true); }
  catch (e) { v.innerHTML = `<div class="reader"><div class="empty-result">${esc(e.message)}</div></div>`; return; }
  if (currentTab()?.key !== t.key) return;
  t.title = clip(s.prompt, 40); renderTabs();
  const running = s.status === "running";
  v.innerHTML = `
    <div class="vbar">
      <button class="btn small" data-a="star">${s.meta.starred ? "\u2605 Starred" : "\u2606 Star"}</button>
      <button class="btn small" data-a="copy">Copy report</button>
      <button class="btn small" data-a="to-nb">\u2192 Notebook</button>
      <button class="btn small" data-a="find">Find</button>
      ${s.result ? `<button class="btn small" data-a="listen" title="Read the report aloud, word for word">\u25B6 Listen</button>` : ""}
      ${s.children.length ? `<button class="btn small" data-a="tree">\u2937 Tree (${s.children.length})</button>` : ""}
      ${s.result && !s.parent_id ? `<button class="btn small" data-a="rerun" title="Run this question again and compare">\u21BB Re-run</button>` : ""}
      ${s.reruns?.length || s.run?.rerun_of ? `<button class="btn small" data-a="compare">\u21C4 Compare</button>` : ""}
      <span class="grow"></span>
      <select class="btn small" data-a="export">
        <option value="">Export\u2026</option>
        <option value="md">Markdown (+annotations)</option>
        <option value="md-rec">Markdown, with sub-reports</option>
        <option value="html">Standalone HTML</option>
        <option value="json">JSON</option>
        <option value="print">Print / PDF</option>
        <option value="audio-full">Audio: full report (AI voice)</option>
        <option value="audio-summary">Audio: AI voice summary</option>
        <option value="brief">Brief: executive / slides / email</option>
      </select>
      ${running ? `<button class="btn small danger" data-a="cancel">Cancel run</button>` : ""}
      <button class="btn small danger" data-a="delete" title="Delete session">Delete</button>
    </div>
    <div class="reader">
      <div class="dossier">
        <h1>${esc(s.prompt)}</h1>
        <div class="meta">
          <span class="status-badge ${esc(s.status)}">${esc(s.status)}</span>
          <span>SESSION <b>#${s.id}</b></span>
          <span>DEPTH <b>${s.depth || 1}</b></span>
          <span>CREATED <b>${esc((s.created_at || "").replace("T", " ").slice(0, 16))}</b></span>
          <span>SIZE <b>${fmtN((s.result || "").length)}</b> chars</span>
          ${s.parent_id ? `<span>PARENT <a href="#" data-open="${s.parent_id}" style="color:var(--cyan)">#${s.parent_id}</a></span>` : ""}
          ${s.files.length ? `<span>FILES <b>${s.files.length}</b></span>` : ""}
        </div>
      </div>
      <div class="listenbar" hidden></div>
      <div class="findbar" hidden>
        <input class="inline-input" placeholder="Find in report\u2026" style="max-width:320px">
        <span class="mono dim find-count" style="align-self:center"></span>
      </div>
      <article class="md" id="report"></article>
    </div>
    <div class="dock">
      <div class="dock-inner">
        <textarea rows="1" placeholder="${running ? "Follow-ups unlock when the run finishes\u2026" : (window.innerWidth < 820 ? "Ask a follow-up about this research\u2026" : "Ask a follow-up about this research\u2026 (Enter to send, Shift+Enter for newline)")}" ${running ? "disabled" : ""}></textarea>
        <button class="btn primary" data-a="ask" ${running ? "disabled" : ""}>Ask</button>
      </div>
    </div>`;
  const art = v.querySelector("#report");
  if (s.result) {
    art.innerHTML = renderMd(s.result);
    art.querySelectorAll("a[href^='http']").forEach((a) => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
    art.querySelectorAll("h1,h2,h3,h4").forEach((h, i) => (h.id = `h-${i}-${slug(h.textContent)}`));
    applyAnnotations(art, s.annotations);
    CITE.decorate(art, s.result);
  } else {
    art.innerHTML = `<div class="empty-result ${running ? "scan" : ""}">${running ? "Research in progress. The live log is streaming in the right panel." : "No result stored for this session."}</div>`;
  }
  if (running && S.rtab !== "log") { S.rtab = "log"; }
  renderRight();

  // toolbar
  v.querySelector('[data-a="star"]').onclick = async () => {
    const m = await api(`/api/sessions/${s.id}/meta`, { method: "PATCH", body: { starred: !s.meta.starred } });
    s.meta = m; toast(m.starred ? "Starred" : "Unstarred"); loadSessions(); renderStage();
  };
  v.querySelector('[data-a="copy"]').onclick = () => copyText(s.result || "");
  v.querySelector('[data-a="to-nb"]').onclick = () => NB.append(`## ${s.prompt}\n\n${s.result || ""}\n\n*Source: Session #${s.id}*\n`);
  v.querySelector('[data-a="tree"]')?.addEventListener("click", () => openTree(s.id));
  v.querySelector('[data-a="listen"]')?.addEventListener("click", () => READER.toggle(v, art, s));
  v.querySelector('[data-a="rerun"]')?.addEventListener("click", async () => {
    const d = s.run?.depth || s.depth || 1, b = s.run?.breadth || 3;
    let est = null; try { est = await api("/api/estimate", { method: "POST", body: { depth: d, breadth: b } }); } catch { /* ignore */ }
    if (!(await confirmBox(`Re-run #${s.id}?`, `Runs the same question again (depth ${d}) so you can compare what changed.${est ? ` Estimated cost about $${est.cost_usd.toFixed(2)}.` : ""}`, "Re-run"))) return;
    try {
      const r = await api("/api/research", { method: "POST", body: { prompt: s.prompt, depth: d, breadth: b, rerun_of: s.id } });
      toast(`Re-run #${r.id} launched`, "ok"); S.rtab = "log"; await loadSessions(); openSession(r.id);
    } catch (e) { toast(e.message, "err"); }
  });
  v.querySelector('[data-a="compare"]')?.addEventListener("click", () => {
    const other = s.run?.rerun_of || s.reruns[s.reruns.length - 1];
    const [a, b] = [Math.min(s.id, other), Math.max(s.id, other)];
    openCompare(a, b);
  });
  v.querySelector("[data-open]")?.addEventListener("click", (e) => { e.preventDefault(); openSession(e.target.dataset.open); });
  v.querySelector('[data-a="export"]').onchange = async (e) => {
    const f = e.target.value; e.target.value = "";
    if (!f) return;
    if (f === "print") return window.print();
    if (f === "audio-full" || f === "audio-summary") return AUDIO.exportDialog("session", s.id, f.slice(6), s.prompt);
    if (f === "brief") return BRIEF.dialog("session", s.id, s.prompt);
    if (f === "html") return download(`session_${s.id}.html`, standaloneHtml(s.prompt, v.querySelector(".dossier").outerHTML + art.outerHTML), "text/html");
    const rec = f === "md-rec" ? "&recursive=1" : "";
    const fmt = f === "json" ? "json" : "md";
    const out = await api(`/api/sessions/${s.id}/export?format=${fmt}${rec}`);
    download(out.filename, fmt === "json" ? JSON.stringify(out.content, null, 2) : out.content, fmt === "json" ? "application/json" : "text/markdown");
  };
  v.querySelector('[data-a="cancel"]')?.addEventListener("click", async () => {
    if (!(await confirmBox("Cancel this run?", "Stops the background process and asks Gemini to cancel the interaction."))) return;
    try { const r = await api(`/api/sessions/${s.id}/cancel`, { method: "POST" }); toast(r.notes.join("; ") || "Cancelled"); }
    catch (err) { toast(err.message, "err"); }
    delete S.cache[s.id]; loadSessions(); renderStage();
  });
  v.querySelector('[data-a="delete"]').onclick = async () => {
    const hasKids = s.children.length > 0;
    const ok = await confirmBox(`Delete session #${s.id}?`, `This removes it from local history${hasKids ? ` along with its ${s.children.length} direct sub-task(s) and their descendants` : ""}, plus its annotations. It cannot be undone.`, "Delete");
    if (!ok) return;
    await api(`/api/sessions/${s.id}${hasKids ? "?recursive=1" : ""}`, { method: "DELETE" });
    toast(`Deleted #${s.id}`, "ok"); delete S.cache[s.id];
    closeTab(t.key); loadSessions(); loadStats();
  };
  // find
  const fb = v.querySelector(".findbar");
  const findInput = fb.querySelector("input");
  v.querySelector('[data-a="find"]').onclick = () => { fb.hidden = !fb.hidden; if (!fb.hidden) findInput.focus(); };
  findInput.oninput = debounce(() => {
    $$("mark.find", art).forEach((m) => m.replaceWith(...m.childNodes));
    art.normalize();
    const q = findInput.value.trim();
    if (q.length < 2) { fb.querySelector(".find-count").textContent = ""; return; }
    const n = wrapText(art, q, () => { const m = document.createElement("mark"); m.className = "find"; return m; }, Infinity);
    fb.querySelector(".find-count").textContent = `${n} match${n === 1 ? "" : "es"}`;
    art.querySelector("mark.find")?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, 200);
  // follow-up dock
  const ta = v.querySelector(".dock textarea");
  const askBtn = v.querySelector('[data-a="ask"]');
  const grow = () => { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 160) + "px"; };
  ta.oninput = grow;
  ta.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); askBtn.click(); } };
  askBtn.onclick = async () => {
    const q = ta.value.trim(); if (!q) return;
    askBtn.disabled = true; askBtn.innerHTML = '<span class="spinner"></span>'; status(`follow-up on #${s.id}\u2026`);
    try {
      await api(`/api/sessions/${s.id}/followup`, { method: "POST", body: { prompt: q } });
      toast("Follow-up added to the report", "ok"); ta.value = "";
      delete S.cache[s.id]; await renderStage();
      setTimeout(() => { const r = $("#report"); r?.lastElementChild?.scrollIntoView({ behavior: "smooth", block: "end" }); }, 100);
    } catch (e) { toast(e.message, "err"); askBtn.disabled = false; askBtn.textContent = "Ask"; }
    status("ready");
  };
  if (S.pendingAsk) { ta.value = S.pendingAsk; S.pendingAsk = null; grow(); ta.focus(); }
}

// Wrap occurrences of `needle` in text nodes under `root`. Returns count wrapped.
// If `onlyIndex` is a number, wraps only that occurrence.
function wrapText(root, needle, make, limit = Infinity, onlyIndex = null) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => n.parentElement.closest("mark.find") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
  });
  const nodes = []; let n;
  while ((n = walker.nextNode())) nodes.push(n);
  const full = nodes.map((x) => x.nodeValue).join("");
  const lowerFull = full.toLowerCase(); const lowerNeedle = needle.toLowerCase();
  const hits = []; let from = 0;
  while (hits.length < limit) {
    const i = lowerFull.indexOf(lowerNeedle, from);
    if (i < 0) break;
    hits.push(i); from = i + Math.max(1, needle.length);
  }
  const targets = onlyIndex === null ? hits : hits.filter((_, k) => k === onlyIndex);
  // Map global offsets to (node, offset) and wrap from the end to keep offsets valid.
  const starts = []; let acc = 0;
  for (const x of nodes) { starts.push(acc); acc += x.nodeValue.length; }
  const loc = (g) => { let k = starts.length - 1; while (k > 0 && starts[k] > g) k--; return [nodes[k], g - starts[k], k]; };
  for (const h of [...targets].reverse()) {
    const end = h + needle.length;
    const [, , ks] = loc(h); const [, , ke] = loc(end - 1);
    for (let k = ke; k >= ks; k--) {
      const node = nodes[k];
      const a = Math.max(h, starts[k]) - starts[k];
      const b = Math.min(end, starts[k] + node.nodeValue.length) - starts[k];
      if (b <= a || !node.nodeValue.slice(a, b).trim()) continue;
      const r = document.createRange();
      r.setStart(node, a); r.setEnd(node, b);
      const m = make(); r.surroundContents(m);
    }
  }
  return targets.length;
}
function applyAnnotations(art, anns) {
  for (const a of anns) {
    const n = wrapText(art, a.quote, () => {
      const m = document.createElement("mark");
      m.className = `ann ${a.color}${a.note ? " has-note" : ""}`;
      m.dataset.ann = a.id; m.title = a.note || "Highlight";
      return m;
    }, Infinity, a.occurrence || 0);
    if (!n) wrapText(art, a.quote, () => { const m = document.createElement("mark"); m.className = `ann ${a.color}`; m.dataset.ann = a.id; return m; }, 1);
  }
  art.onclick = (e) => {
    const m = e.target.closest("mark.ann");
    if (!m) return;
    S.rtab = "notes"; renderRight();
    setTimeout(() => $(`.ann-card[data-id="${m.dataset.ann}"] textarea`)?.focus(), 50);
  };
}

// ---------------------------------------------------------------- selection toolbar
let SEL = null;
function hideSelbar() { $("#selbar").hidden = true; SEL = null; }
document.addEventListener("mouseup", (e) => {
  if (e.target.closest("#selbar")) return;
  setTimeout(() => {
    const sel = window.getSelection();
    const art = $("#report") || $(".nb .preview");
    const text = sel?.toString().trim();
    if (!text || !art || sel.rangeCount === 0 || !art.contains(sel.anchorNode)) return hideSelbar();
    const range = sel.getRangeAt(0);
    const rect = range.getBoundingClientRect();
    // occurrence index: how many times the text appears before the selection start
    const pre = document.createRange();
    pre.selectNodeContents(art); pre.setEnd(range.startContainer, range.startOffset);
    const before = pre.toString().toLowerCase();
    const needle = text.toLowerCase();
    let occ = 0, i = -1;
    while ((i = before.indexOf(needle, i + 1)) >= 0) occ++;
    SEL = { text, occurrence: occ, inNotebook: !$("#report") };
    const bar = $("#selbar");
    bar.hidden = false;
    $$('[data-act="hl"],[data-act="note"],[data-act="ask"]', bar).forEach((b) => (b.style.display = SEL.inNotebook ? "none" : ""));
    bar.querySelector(".sep").style.display = SEL.inNotebook ? "none" : "";
    const bw = bar.offsetWidth;
    bar.style.left = Math.max(8, Math.min(window.innerWidth - bw - 8, rect.left + rect.width / 2 - bw / 2)) + "px";
    bar.style.top = Math.max(60, rect.top - 44) + "px";
  }, 10);
});
document.addEventListener("scroll", hideSelbar, true);
$("#selbar").addEventListener("click", async (e) => {
  const b = e.target.closest("button"); if (!b || !SEL) return;
  const t = currentTab();
  const sid = t?.kind === "session" ? t.id : null;
  const act = b.dataset.act;
  if (act === "copy") { await copyText(SEL.text); }
  else if (act === "quote") {
    const src = sid ? `\n>\n> *Session #${sid}*` : "";
    await NB.append("> " + SEL.text.replace(/\n+/g, "\n> ") + src + "\n");
  } else if (act === "ask" && sid) {
    S.pendingAsk = `Regarding this passage: "${clip(SEL.text, 600)}"\n\n`;
    const ta = $(".dock textarea"); if (ta) { ta.value = S.pendingAsk; S.pendingAsk = null; ta.dispatchEvent(new Event("input")); ta.focus(); }
  } else if ((act === "hl" || act === "note") && sid) {
    try {
      const a = await api("/api/annotations", { method: "POST", body: { session_id: sid, quote: SEL.text, occurrence: SEL.occurrence, color: b.dataset.color || "amber" } });
      const s = S.cache[sid]; s.annotations.push(a);
      window.getSelection().removeAllRanges();
      applyAnnotations($("#report"), [a]);
      S.rtab = "notes"; renderRight(); loadStats();
      if (act === "note") setTimeout(() => $(`.ann-card[data-id="${a.id}"] textarea`)?.focus(), 50);
    } catch (err) { toast(err.message, "err"); }
  }
  hideSelbar();
});

// ---------------------------------------------------------------- notebook
const NB = {
  dirty: false,
  timer: null,
  current: null,
  async create(title, content = "") {
    const nb = await api("/api/notebooks", { method: "POST", body: { title: title || `Notebook ${new Date().toISOString().slice(0, 10)}`, content } });
    S.activeNotebook = nb.id; await loadNotebooks(); loadStats();
    openNotebook(nb.id, nb.title);
    return nb;
  },
  async target() {
    if (S.activeNotebook) { try { return await api(`/api/notebooks/${S.activeNotebook}`); } catch { S.activeNotebook = null; } }
    await loadNotebooks();
    if (S.notebooks.length) { S.activeNotebook = S.notebooks[0].id; return api(`/api/notebooks/${S.activeNotebook}`); }
    const nb = await api("/api/notebooks", { method: "POST", body: { title: "Research notebook" } });
    S.activeNotebook = nb.id; await loadNotebooks();
    return nb;
  },
  async append(md) {
    if (NB.dirty) await NB.saveNow();
    const nb = await NB.target();
    const content = (nb.content ? nb.content.replace(/\s*$/, "") + "\n\n" : "") + md;
    await api(`/api/notebooks/${nb.id}`, { method: "PUT", body: { content } });
    toast(`Added to \u201c${nb.title}\u201d`, "ok");
    const ta = $(".nb textarea");
    if (ta && NB.current === nb.id) { ta.value = content; NB.preview(); }
  },
  async saveNow() {
    clearTimeout(NB.timer);
    const ta = $(".nb textarea"); const ti = $(".nb-title");
    if (!ta || !NB.current) { NB.dirty = false; return; }
    const nb = await api(`/api/notebooks/${NB.current}`, { method: "PUT", body: { content: ta.value, title: ti.value } });
    NB.dirty = false;
    const st = $(".save-state"); if (st) { st.textContent = `saved ${new Date().toLocaleTimeString()}`; st.className = "save-state saved"; }
    const tab = S.tabs.find((t) => t.key === `n${nb.id}`); if (tab && tab.title !== nb.title) { tab.title = nb.title; saveTabs(); renderTabs(); }
  },
  preview() {
    const ta = $(".nb textarea"); const pv = $(".nb .preview");
    if (!ta || !pv) return;
    pv.innerHTML = `<div class="md">${renderMd(ta.value)}</div>`;
    const md = pv.querySelector(".md");
    md.querySelectorAll("a[href^='http']").forEach((a) => { a.target = "_blank"; a.rel = "noopener noreferrer"; });
    CITE.decorate(md, ta.value, { collapseSources: true, noUncited: true });
    if (!md.querySelector(".cite")) CITE.collapseSources(md);
  },
};
async function renderNotebook(v, t) {
  let nb;
  try { nb = await api(`/api/notebooks/${t.id}`); }
  catch (e) { v.innerHTML = `<div class="reader"><div class="empty-result">${esc(e.message)}</div></div>`; return; }
  NB.current = nb.id; S.activeNotebook = nb.id; NB.dirty = false;
  const mode = localStorage.getItem("dr.nbmode") || "split";
  v.innerHTML = `
    <div class="vbar">
      <input class="nb-title" value="${esc(nb.title)}">
      <span class="save-state">saved</span>
      <span class="grow"></span>
      <div class="seg" id="nbmode">
        <button data-m="edit">Edit</button><button data-m="split">Split</button><button data-m="preview">Read</button>
      </div>
      <button class="btn small" data-a="copy">Copy</button>
      <select class="btn small" data-a="export">
        <option value="">Export\u2026</option><option value="md">Markdown</option><option value="html">Standalone HTML</option><option value="print">Print / PDF</option>
        <option value="brief">Brief: executive / slides / email</option><option value="audio-full">Audio: read notebook (AI voice)</option><option value="audio-summary">Audio: AI voice summary</option>
      </select>
      <button class="btn small danger" data-a="del">Delete</button>
    </div>
    <div class="nb mode-${mode}">
      <textarea spellcheck="true" placeholder="# Working notes\n\nSelect text in any report and hit \u2192 Notebook to collect quotes here with their source.\nMarkdown is supported. Autosaves as you type. Ctrl+S saves now.">${esc(nb.content)}</textarea>
      <div class="preview"></div>
    </div>`;
  const ta = v.querySelector("textarea");
  const ti = v.querySelector(".nb-title");
  NB.preview();
  $$("#nbmode button", v).forEach((b) => {
    b.classList.toggle("on", b.dataset.m === mode);
    b.onclick = () => { localStorage.setItem("dr.nbmode", b.dataset.m); v.querySelector(".nb").className = `nb mode-${b.dataset.m}`; $$("#nbmode button", v).forEach((x) => x.classList.toggle("on", x === b)); };
  });
  const dirty = () => {
    NB.dirty = true; const st = v.querySelector(".save-state"); st.textContent = "unsaved"; st.className = "save-state dirty";
    clearTimeout(NB.timer); NB.timer = setTimeout(() => NB.saveNow().catch((e) => toast(e.message, "err")), 900);
  };
  ta.oninput = () => { dirty(); NB.previewSoon(); };
  ti.oninput = dirty;
  ta.onkeydown = (e) => {
    if (e.key === "Tab") { e.preventDefault(); const s = ta.selectionStart; ta.setRangeText("  ", s, ta.selectionEnd, "end"); dirty(); }
  };
  v.querySelector('[data-a="copy"]').onclick = () => copyText(ta.value);
  v.querySelector('[data-a="export"]').onchange = (e) => {
    const f = e.target.value; e.target.value = "";
    const name = slug(ti.value) || "notebook";
    if (f === "md") download(`${name}.md`, ta.value);
    else if (f === "html") download(`${name}.html`, standaloneHtml(ti.value, `<div class="md">${renderMd(ta.value)}</div>`), "text/html");
    else if (f === "print") { v.querySelector(".nb").className = "nb mode-preview"; setTimeout(() => window.print(), 50); }
    else if (f === "brief" || f.startsWith("audio")) {
      NB.saveNow().then(() => f === "brief" ? BRIEF.dialog("notebook", nb.id, ti.value) : AUDIO.exportDialog("notebook", nb.id, f.slice(6), ti.value));
    }
  };
  v.querySelector('[data-a="del"]').onclick = async () => {
    if (!(await confirmBox(`Delete notebook \u201c${ti.value}\u201d?`, "This cannot be undone.", "Delete"))) return;
    await api(`/api/notebooks/${nb.id}`, { method: "DELETE" });
    NB.dirty = false; NB.current = null; if (S.activeNotebook === nb.id) S.activeNotebook = null;
    toast("Notebook deleted", "ok"); closeTab(t.key); loadNotebooks(); loadStats();
  };
}
NB.previewSoon = debounce(() => NB.preview(), 250);

// ---------------------------------------------------------------- launch
const TEMPLATES = [
  ["Market scan", "Map the competitive landscape for [TOPIC]: key players, market size, pricing, differentiators, and recent moves. Cite sources."],
  ["Literature review", "Write a literature review of [TOPIC] covering foundational work, the current state of the art, open problems, and key research groups. Cite papers."],
  ["Tech deep dive", "Explain how [TECHNOLOGY] works, its architecture, trade-offs versus alternatives, adoption, and known failure modes."],
  ["Due diligence", "Due-diligence brief on [COMPANY]: business model, leadership, financials, customers, risks, controversies, and recent news."],
  ["Policy brief", "Policy brief on [ISSUE]: background, stakeholders, current regulations, arguments for and against, and likely developments."],
  ["Compare", "Compare [A] vs [B] vs [C] on cost, performance, maturity, ecosystem, and fit for [USE CASE]. End with a recommendation table."],
];
function renderLaunch(v) {
  const pre = S.launchPrefill || {};
  const noKey = S.health && !S.health.api_key;
  v.innerHTML = `
  <div class="pad" style="max-width:860px">
    <div class="hero" style="grid-template-columns:1fr;margin-bottom:14px">
      <div><h2>New <span>deep research</span></h2>
      <p>Runs in the background as a normal <span class="mono">deep-research</span> session, so it survives closing this page and shows up in <span class="mono">deep-research list</span>.</p></div>
    </div>
    ${noKey ? `<p class="warn">GEMINI_API_KEY is not visible to the dashboard process. Run <span class="mono">deep-research auth login</span>, then <span class="mono">deep-research dashboard --restart</span>.</p>` : ""}
    <div class="templates">${TEMPLATES.map(([n], i) => `<button data-t="${i}">${esc(n)}</button>`).join("")}</div>
    <div class="field"><label>Research objective</label>
      <textarea id="l-prompt" placeholder="What do you want to know? Be specific about scope, timeframe, and what the output should contain.">${esc(pre.prompt || "")}</textarea></div>
    <div class="row">
      <div class="field"><label>Depth (recursion)</label>
        <div class="range-wrap"><input type="range" id="l-depth" min="1" max="4" value="1"><output id="o-depth">1</output></div></div>
      <div class="field"><label>Breadth (sub-tasks per level)</label>
        <div class="range-wrap"><input type="range" id="l-breadth" min="1" max="6" value="3"><output id="o-breadth">3</output></div></div>
    </div>
    <div class="field"><label>Output format (optional)</label>
      <input id="l-format" placeholder='e.g. "Executive summary, then a Markdown comparison table"'></div>
    <div class="field"><label>Your documents (optional)</label>
      <div class="drop" id="l-drop">Drop files here or click to choose. They are uploaded to a temporary File Search Store for this run.</div>
      <input type="file" id="l-file" multiple hidden>
      <div class="filelist" id="l-files"></div></div>
    <div class="field"><label>Existing File Search Stores (optional, space separated)</label>
      <input id="l-stores" placeholder="fileSearchStores/abc123"></div>
    <div class="estimate" id="l-est"></div>
    <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">
      <button class="btn primary" id="l-go" ${noKey ? "disabled" : ""}>Launch research \u2192</button>
    </div>
  </div>`;
  const uploads = [];
  const est = debounce(async () => {
    const d = +$("#l-depth").value, b = +$("#l-breadth").value;
    $("#o-depth").textContent = d; $("#o-breadth").textContent = b;
    $("#l-breadth").disabled = d === 1;
    try {
      const e = await api("/api/estimate", { method: "POST", body: { depth: d, breadth: b, uploads: uploads.map((u) => u.path) } });
      $("#l-est").innerHTML = `<span>AGENT RUNS <b>${e.nodes}</b></span><span>INPUT <b>${fmtN(e.input_tokens)}</b> tok</span><span>OUTPUT <b>${fmtN(e.output_tokens)}</b> tok</span><span>EST. COST <b>$${e.cost_usd.toFixed(2)}</b></span><span class="dim">rough, same model as <span class="mono">deep-research estimate</span></span>`;
    } catch (err) { $("#l-est").textContent = err.message; }
  }, 120);
  ["l-depth", "l-breadth"].forEach((id) => ($("#" + id).oninput = est));
  est();
  $$(".templates button", v).forEach((b) => (b.onclick = () => { const ta = $("#l-prompt"); ta.value = TEMPLATES[b.dataset.t][1]; ta.focus(); const i = ta.value.indexOf("["); if (i >= 0) ta.setSelectionRange(i, ta.value.indexOf("]", i) + 1); }));
  const renderFiles = () => {
    $("#l-files").innerHTML = uploads.map((u, i) => `<span class="filechip">${esc(u.name)} <span class="dim">${fmtN(Math.round(u.size / 1024))}KB</span><button data-rm="${i}">\u00d7</button></span>`).join("");
    $$("[data-rm]", v).forEach((b) => (b.onclick = () => { uploads.splice(+b.dataset.rm, 1); renderFiles(); est(); }));
  };
  const addFiles = async (files) => {
    for (const f of files) {
      if (f.size > 18 * 1024 * 1024) { toast(`${f.name} is over 18 MB`, "err"); continue; }
      status(`uploading ${f.name}\u2026`);
      const data = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(String(r.result).split(",")[1] || ""); r.onerror = rej; r.readAsDataURL(f); });
      try { uploads.push(await api("/api/uploads", { method: "POST", body: { name: f.name, data } })); }
      catch (e) { toast(e.message, "err"); }
    }
    status("ready"); renderFiles(); est();
  };
  const drop = $("#l-drop");
  drop.onclick = () => $("#l-file").click();
  $("#l-file").onchange = (e) => addFiles([...e.target.files]);
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); addFiles([...e.dataTransfer.files]); };
  $("#l-go").onclick = async () => {
    const prompt = $("#l-prompt").value.trim();
    if (!prompt) return toast("Write a research objective first", "err");
    const d = +$("#l-depth").value;
    if (d > 1 && !(await confirmBox("Launch recursive research?", `Depth ${d} runs ${$("#l-est").querySelector("b")?.textContent || "several"} agent tasks. Check the cost estimate.`, "Launch"))) return;
    $("#l-go").disabled = true; $("#l-go").innerHTML = '<span class="spinner"></span> Launching';
    try {
      const r = await api("/api/research", { method: "POST", body: {
        prompt, depth: d, breadth: +$("#l-breadth").value, format: $("#l-format").value,
        uploads: uploads.map((u) => u.path), stores: $("#l-stores").value.split(/\s+/).filter(Boolean),
      } });
      toast(`Research #${r.id} launched`, "ok");
      NOTIFY.ask();
      S.launchPrefill = null;
      closeTab("launch"); S.rtab = "log";
      await loadSessions(); loadStats();
      openSession(r.id);
    } catch (e) { toast(e.message, "err"); $("#l-go").disabled = false; $("#l-go").textContent = "Launch research \u2192"; }
  };
  $("#l-prompt").focus();
}

// ---------------------------------------------------------------- semantic search
function renderSearch(v) {
  v.innerHTML = `
  <div class="pad" style="max-width:900px">
    <div class="hero" style="grid-template-columns:1fr;margin-bottom:14px"><div>
      <h2>Search your <span>research memory</span></h2>
      <p>Semantic search across completed sessions, then a cited answer synthesized only from your own past research. The first search embeds any sessions that are not indexed yet.</p></div></div>
    <div class="dock-inner" style="margin-bottom:14px">
      <textarea id="sq" rows="1" placeholder="What did I find about\u2026"></textarea>
      <label class="mono dim" style="display:flex;align-items:center;gap:4px;font-size:11px"><input type="checkbox" id="ssyn" checked> synthesize</label>
      <button class="btn primary" id="sgo">Search</button>
    </div>
    <div id="sres"></div>
  </div>`;
  const go = async () => {
    const q = $("#sq").value.trim(); if (!q) return;
    $("#sgo").disabled = true;
    $("#sres").innerHTML = `<div class="empty-result scan">Embedding and ranking\u2026</div>`;
    try {
      const r = await api("/api/search", { method: "POST", body: { query: q, limit: 6, synthesize: $("#ssyn").checked } });
      $("#sres").innerHTML = `
        ${r.embedded_now ? `<p class="dim mono">Indexed ${r.embedded_now} new session(s).</p>` : ""}
        ${r.answer ? `<div class="card" style="margin-bottom:14px"><h3>Synthesized answer</h3><div class="md" id="sans">${renderMd(r.answer)}</div>
           <div style="margin-top:10px;display:flex;gap:6px"><button class="btn small" id="scopy">Copy</button><button class="btn small" id="snb">\u2192 Notebook</button></div></div>` : ""}
        <div class="card"><h3>Matches</h3>${r.matches.map((m) => `
          <div class="match" data-id="${m.id}"><div class="score">${(m.score * 100).toFixed(1)}</div>
          <div style="flex:1"><div>${esc(clip(m.prompt, 200))}</div><div class="bar"><i style="width:${Math.max(4, m.score * 100)}%"></i></div></div>
          <div class="mono dim">#${m.id}</div></div>`).join("") || '<div class="dim">No indexed sessions.</div>'}</div>`;
      $$(".match", v).forEach((m) => (m.onclick = () => openSession(m.dataset.id)));
      $("#sans")?.addEventListener("click", (e) => {
        const a = e.target.textContent.match(/Session #(\d+)/); if (a) openSession(a[1]);
      });
      $("#scopy")?.addEventListener("click", () => copyText(r.answer));
      $("#snb")?.addEventListener("click", () => NB.append(`### Search: ${q}\n\n${r.answer}\n`));
    } catch (e) { $("#sres").innerHTML = `<div class="empty-result">${esc(e.message)}</div>`; }
    $("#sgo").disabled = false;
  };
  $("#sgo").onclick = go;
  $("#sq").onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); go(); } };
  $("#sq").focus();
}

// ---------------------------------------------------------------- tree
async function renderTree(v, t) {
  v.innerHTML = `<div class="pad"><div class="empty-result scan">Loading tree\u2026</div></div>`;
  const tree = await api(`/api/sessions/${t.id}/tree`);
  const node = (n) => `<li><div class="tnode" data-id="${n.id}"><span class="st ${esc(n.status)}"></span>
    <div><div class="mono dim" style="font-size:10.5px">#${n.id} \u00b7 depth ${n.depth} \u00b7 ${esc(n.status)}</div><div>${esc(clip(n.prompt, 180))}</div></div></div>
    ${n.children.length ? `<ul>${n.children.map(node).join("")}</ul>` : ""}</li>`;
  v.innerHTML = `<div class="vbar"><span class="title">Research tree</span><span class="grow"></span>
    <button class="btn small" id="t-rec">Export full report (Markdown)</button></div>
    <div class="pad tree"><ul>${node(tree)}</ul></div>`;
  $$(".tnode", v).forEach((n) => (n.onclick = () => openSession(n.dataset.id)));
  $("#t-rec").onclick = async () => { const o = await api(`/api/sessions/${t.id}/export?format=md&recursive=1`); download(o.filename.replace(".md", "_tree.md"), o.content); };
}

// ---------------------------------------------------------------- right inspector
let LOG = { sid: null, offset: 0, timer: null };
function renderRight() {
  $$("#rtabs button").forEach((b) => b.classList.toggle("on", b.dataset.r === S.rtab));
  const body = $("#rbody");
  const t = currentTab();
  const s = t?.kind === "session" ? S.cache[t.id] : null;
  if (S.rtab !== "log") stopLog();
  if (!s) {
    if (t?.kind === "notebook") {
      body.innerHTML = `<div class="dim">Notebook autosaves to the local history database. Select text in its preview to copy or quote it. <span class="mono">Ctrl+S</span> saves now.</div>`;
      return;
    }
    const live = S.sessions.filter((x) => x.status === "running");
    body.innerHTML = `
      <div class="label">Live runs</div>
      <div class="children" style="margin:6px 0 16px">${live.map((x) => `<a data-id="${x.id}"><span class="st running" style="display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:6px"></span>#${x.id} ${esc(clip(x.prompt, 80))}<br><span class="mono dim" style="font-size:10.5px">started ${ago(x.created_at)}</span></a>`).join("") || '<div class="dim">Nothing running.</div>'}</div>
      <div class="label">Keyboard</div>
      <div class="kv" style="margin-top:8px">
        <span class="k">Ctrl K</span><span class="v">command palette</span>
        <span class="k">Ctrl F</span><span class="v">find in report</span>
        <span class="k">Ctrl S</span><span class="v">save notebook</span>
        <span class="k">select</span><span class="v">highlight, note, quote to notebook, ask</span>
      </div>
      <div class="dim" style="margin-top:16px;font-size:11.5px">Open a session to see its intel, annotations, outline and live log.</div>`;
    $$(".children a", body).forEach((a) => (a.onclick = () => openSession(a.dataset.id)));
    return;
  }
  if (S.rtab === "info") {
    body.innerHTML = `
      <div class="kv">
        <span class="k">session</span><span class="v">#${s.id}</span>
        <span class="k">status</span><span class="v"><span class="status-badge ${esc(s.status)}">${esc(s.status)}</span></span>
        <span class="k">interaction</span><span class="v"><span class="trunc" title="${esc(s.interaction_id)}">${esc(clip(s.interaction_id, 22))}</span> <button class="linkbtn" id="copy-iid">copy</button></span>
        <span class="k">created</span><span class="v">${esc(niceTime(s.created_at))}</span>
        <span class="k">updated</span><span class="v">${esc(niceTime(s.updated_at))}</span>
        <span class="k">depth</span><span class="v">${s.depth || 1}</span>
        ${s.status === "running" && s.pid ? `<span class="k">pid</span><span class="v">${s.pid}</span>` : ""}
        <span class="k">words</span><span class="v">${fmtN((s.result || "").split(/\s+/).filter(Boolean).length)}</span>
        <span class="k">sources</span><span class="v">${fmtN(extractSources(s.result || "").length)} unique</span>
        <span class="k">cost</span><span class="v" id="cost-v"><span class="dim">\u2026</span></span>
      </div>
      <div id="cost-detail" class="mono dim" style="font-size:10.5px;margin-top:4px"></div>
      <div class="section label">Audio</div><div id="audio-list" class="dim" style="font-size:11.5px">\u2026</div>
      <div class="section label">Tags</div>
      <div class="tags-edit" id="tags">${s.meta.tags.map((tg, i) => `<span class="tagpill">${esc(tg)}<button data-i="${i}">\u00d7</button></span>`).join("")}
        <input placeholder="+ tag" id="tag-in"></div>
      ${s.files.length ? `<div class="section label">Files</div>${s.files.map((f) => `<div class="mono dim" style="font-size:11px;word-break:break-all">${esc(f)}</div>`).join("")}` : ""}
      ${s.children.length ? `<div class="section label">Sub-tasks</div><div class="children">${s.children.map((c) => `<a data-id="${c.id}"><span class="st ${esc(c.status)}" style="display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:6px"></span>#${c.id} ${esc(clip(c.prompt, 90))}</a>`).join("")}</div>` : ""}
      <div class="section label">Sources</div>
      <div id="srcs"></div>`;
    const srcs = extractSources(s.result || "");
    $("#srcs").innerHTML = srcs.slice(0, 120).map((x) => `<a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer" class="src" title="${esc(x.url)}"><span>${esc(x.label)}</span>${x.n > 1 ? `<i>${x.n}</i>` : ""}</a>`).join("") || '<div class="dim">No links found in the report.</div>';
    $("#copy-iid").onclick = () => copyText(s.interaction_id || "");
    COST.fill(s);
    AUDIO.fillList("session", s.id);
    $$(".children a", body).forEach((a) => (a.onclick = () => openSession(a.dataset.id)));
    const saveTags = async (tags) => { s.meta = await api(`/api/sessions/${s.id}/meta`, { method: "PATCH", body: { tags } }); renderRight(); loadSessions(); };
    $("#tag-in").onkeydown = (e) => { if (e.key === "Enter" && e.target.value.trim()) saveTags([...s.meta.tags, e.target.value.trim()]); };
    $$("#tags button", body).forEach((b) => (b.onclick = () => saveTags(s.meta.tags.filter((_, i) => i !== +b.dataset.i))));
  } else if (S.rtab === "notes") {
    body.innerHTML = `
      <div class="dim" style="font-size:11.5px;margin-bottom:8px">Select text in the report to highlight it or attach a note.</div>
      ${s.annotations.map((a) => `
        <div class="ann-card ${esc(a.color)}" data-id="${a.id}">
          <div class="q">\u201c${esc(a.quote)}\u201d</div>
          <textarea placeholder="Add a note\u2026">${esc(a.note)}</textarea>
          <div class="acts"><button data-a="nb">\u2192 notebook</button><button data-a="copy">copy</button><button data-a="del">delete</button></div>
        </div>`).join("") || '<div class="empty-result" style="padding:18px">No annotations yet.</div>'}
      ${s.annotations.length ? '<button class="btn small" id="all-nb" style="margin-top:8px">Send all to notebook</button>' : ""}`;
    $$(".ann-card", body).forEach((card) => {
      const a = s.annotations.find((x) => x.id == card.dataset.id);
      card.querySelector(".q").onclick = () => { const m = $(`mark.ann[data-ann="${a.id}"]`); if (m) { m.scrollIntoView({ block: "center", behavior: "smooth" }); m.classList.add("flash"); setTimeout(() => m.classList.remove("flash"), 1400); } };
      const ta = card.querySelector("textarea");
      ta.oninput = debounce(async () => {
        const u = await api(`/api/annotations/${a.id}`, { method: "PATCH", body: { note: ta.value } });
        Object.assign(a, u);
        $$(`mark.ann[data-ann="${a.id}"]`).forEach((m) => { m.classList.toggle("has-note", !!u.note); m.title = u.note || "Highlight"; });
      }, 500);
      card.querySelector('[data-a="copy"]').onclick = () => copyText(`> ${a.quote}\n\n${a.note}`);
      card.querySelector('[data-a="nb"]').onclick = () => NB.append(`> ${a.quote.replace(/\n+/g, "\n> ")}\n>\n> *Session #${s.id}*\n\n${a.note}\n`);
      card.querySelector('[data-a="del"]').onclick = async () => {
        await api(`/api/annotations/${a.id}`, { method: "DELETE" });
        s.annotations = s.annotations.filter((x) => x.id !== a.id);
        $$(`mark.ann[data-ann="${a.id}"]`).forEach((m) => m.replaceWith(...m.childNodes));
        renderRight(); loadStats();
      };
    });
    $("#all-nb")?.addEventListener("click", () => NB.append(`## Notes on: ${s.prompt}\n\n` + s.annotations.map((a) => `> ${a.quote.replace(/\n+/g, "\n> ")}\n\n${a.note}`).join("\n\n") + `\n\n*Source: Session #${s.id}*\n`));
  } else if (S.rtab === "outline") {
    const hs = $$("#report h1, #report h2, #report h3, #report h4");
    body.innerHTML = `<div class="outline">${hs.map((h) => `<a href="#" data-h="${h.id}" style="--lvl:${+h.tagName[1] - 1}">${esc(h.textContent)}</a>`).join("") || '<div class="dim">No headings in this report.</div>'}</div>`;
    $$(".outline a", body).forEach((a) => (a.onclick = (e) => { e.preventDefault(); document.getElementById(a.dataset.h)?.scrollIntoView({ behavior: "smooth", block: "start" }); }));
  } else if (S.rtab === "log") {
    body.innerHTML = `<div id="timeline"></div>
      <details class="rawlog" ${s.status === "running" ? "" : ""}><summary class="label">Raw log: session_${s.id}.log ${s.status === "running" ? '<span class="spinner" style="margin-left:6px"></span>' : ""}</summary><div class="log" id="log"></div></details>`;
    TIMELINE.start(s);
    startLog(s);
  }
}
function colorLog(text) {
  return esc(text)
    .replace(/^(.*\[THOUGHT\].*)$/gm, '<span class="th">$1</span>')
    .replace(/^(.*\[INFO\].*)$/gm, '<span class="in">$1</span>')
    .replace(/^(.*\[(?:ERROR|CRITICAL ERROR)\].*)$/gm, '<span class="er">$1</span>')
    .replace(/^(.*\[WARN\].*)$/gm, '<span class="wa">$1</span>');
}
function stopLog() { clearTimeout(LOG.timer); LOG.sid = null; TIMELINE.stop(); }
async function startLog(s) {
  stopLog();
  LOG = { sid: s.id, offset: 0, timer: null };
  const el = $("#log");
  const tick = async () => {
    if (LOG.sid !== s.id || !$("#log")) return;
    try {
      const r = await api(`/api/sessions/${s.id}/log?offset=${LOG.offset}`);
      if (!r.exists) { el.innerHTML = '<span class="dim">No log file for this session (runs started with `research` in a terminal log to that terminal).</span>'; }
      else if (r.text) {
        const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
        if (LOG.offset === 0) el.innerHTML = "";
        el.insertAdjacentHTML("beforeend", colorLog(r.text));
        if (stick) el.scrollTop = el.scrollHeight;
      }
      LOG.offset = r.size || LOG.offset;
      // detect completion so the reader refreshes itself
      const fresh = S.sessions.find((x) => x.id === s.id);
      if (s.status === "running" && fresh && fresh.status !== "running") {
        delete S.cache[s.id]; toast(`Session #${s.id} ${fresh.status}`, fresh.status === "completed" ? "ok" : "err");
        S.rtab = "info"; renderStage(); return;
      }
    } catch { /* transient */ }
    LOG.timer = setTimeout(tick, s.status === "running" ? 2000 : 15000);
  };
  tick();
}

// ---------------------------------------------------------------- modal + palette
function confirmBox(title, body, okLabel = "Confirm") {
  return new Promise((resolve) => {
    $("#modal").innerHTML = `<h3>${esc(title)}</h3><div class="dim">${esc(body)}</div>
      <div class="acts"><button class="btn" data-x="0">Cancel</button><button class="btn ${okLabel === "Delete" ? "danger" : "primary"}" data-x="1">${esc(okLabel)}</button></div>`;
    $("#modal-back").hidden = false;
    const done = (v) => { $("#modal-back").hidden = true; document.removeEventListener("keydown", key); resolve(v); };
    const key = (e) => { if (e.key === "Escape") done(false); if (e.key === "Enter") done(true); };
    document.addEventListener("keydown", key);
    $$("#modal [data-x]").forEach((b) => (b.onclick = () => done(b.dataset.x === "1")));
    $("#modal-back").onclick = (e) => { if (e.target.id === "modal-back") done(false); };
    $('#modal [data-x="1"]').focus();
  });
}
const PAL = { items: [], idx: 0 };
function paletteItems(q) {
  const cmds = [
    ["cmd", "New research", () => openLaunch()],
    ["cmd", "Semantic search", openSearch],
    ["cmd", "New notebook", () => NB.create()],
    ["cmd", "Research map", openMap],
    ["cmd", "Mission control", () => openTab({ key: "home", kind: "home", title: "Mission control" })],
    ["cmd", "Refresh archive", () => { loadSessions(); loadStats(); }],
    ...S.notebooks.map((n) => ["notebook", n.title, () => openNotebook(n.id, n.title)]),
    ...S.sessions.slice(0, 400).map((s) => ["#" + s.id, s.prompt, () => openSession(s.id)]),
  ];
  const ql = q.toLowerCase();
  return (ql ? cmds.filter(([g, t]) => (g + " " + t).toLowerCase().includes(ql)) : cmds).slice(0, 40);
}
function openPalette() {
  $("#palette-back").hidden = false;
  const inp = $("#palette-input"); inp.value = ""; PAL.idx = 0; drawPalette(); inp.focus();
}
function drawPalette() {
  PAL.items = paletteItems($("#palette-input").value);
  $("#palette-list").innerHTML = PAL.items.map(([g, t], i) => `<div class="pitem ${i === PAL.idx ? "on" : ""}" data-i="${i}"><span class="g">${esc(g)}</span><span class="t">${esc(clip(t, 110))}</span></div>`).join("");
  $$(".pitem").forEach((p) => (p.onclick = () => runPalette(+p.dataset.i)));
}
function runPalette(i) { const it = PAL.items[i]; $("#palette-back").hidden = true; if (it) it[2](); }
$("#palette-input").addEventListener("input", () => { PAL.idx = 0; drawPalette(); });
$("#palette-input").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { PAL.idx = Math.min(PAL.idx + 1, PAL.items.length - 1); drawPalette(); e.preventDefault(); }
  else if (e.key === "ArrowUp") { PAL.idx = Math.max(PAL.idx - 1, 0); drawPalette(); e.preventDefault(); }
  else if (e.key === "Enter") runPalette(PAL.idx);
  else if (e.key === "Escape") $("#palette-back").hidden = true;
});
$("#palette-back").addEventListener("click", (e) => { if (e.target.id === "palette-back") $("#palette-back").hidden = true; });

// ---------------------------------------------------------------- wiring
// Drawers for narrow screens (archive left, inspector right).
function drawer(side) {
  const b = document.body, cls = `show-${side}`, other = side === "left" ? "show-right" : "show-left";
  b.classList.remove(other);
  b.classList.toggle(cls);
  if (side === "right" && b.classList.contains(cls)) renderRight();
}
function closeDrawers() { document.body.classList.remove("show-left", "show-right"); }
$("#btn-left").onclick = () => drawer("left");
$("#btn-right").onclick = () => drawer("right");
$("#scrim").onclick = closeDrawers;
$("#btn-new").onclick = () => { closeDrawers(); openLaunch(); };
$("#btn-palette").onclick = openPalette;
$("#q").addEventListener("input", debounce((e) => { S.q = e.target.value.trim(); loadSessions(); }, 250));
$("#roots-only").onchange = (e) => { S.rootsOnly = e.target.checked; renderSessionList(); };
$("#filter-seg").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  S.filter = b.dataset.f; $$("#filter-seg button").forEach((x) => x.classList.toggle("on", x === b)); renderSessionList();
});
$("#session-list").addEventListener("click", (e) => { const el = e.target.closest(".sess"); if (el) { closeDrawers(); openSession(el.dataset.id); } });
$("#tabs").addEventListener("click", (e) => {
  const x = e.target.closest("[data-close]"); if (x) { e.stopPropagation(); return closeTab(x.dataset.close); }
  const t = e.target.closest(".tab"); if (t) { if (NB.dirty) NB.saveNow(); S.active = t.dataset.key; saveTabs(); renderTabs(); renderStage(); }
});
$("#tabs").addEventListener("auxclick", (e) => { const t = e.target.closest(".tab"); if (e.button === 1 && t) closeTab(t.dataset.key); });
$("#rtabs").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) { S.rtab = b.dataset.r; renderRight(); } });
document.addEventListener("keydown", (e) => {
  const mod = e.ctrlKey || e.metaKey;
  if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); }
  else if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); if (currentTab()?.kind === "notebook") NB.saveNow().then(() => toast("Saved", "ok")); }
  else if (mod && e.key.toLowerCase() === "f" && currentTab()?.kind === "session") { e.preventDefault(); $('[data-a="find"]')?.click(); }
  else if (e.key === "Escape") hideSelbar();
});
window.addEventListener("beforeunload", (e) => { if (NB.dirty) { NB.saveNow(); e.preventDefault(); } });

// ---------------------------------------------------------------- boot
(async function boot() {
  try { S.health = await api("/api/health"); $("#version").textContent = "v" + S.health.version; }
  catch { toast("Dashboard API unreachable", "err"); }
  await Promise.all([loadSessions(), loadStats(), loadNotebooks()]).catch((e) => toast(e.message, "err"));
  try {
    const saved = JSON.parse(localStorage.getItem(LS_TABS) || "null");
    if (saved?.tabs?.length) { S.tabs = saved.tabs; S.active = saved.active; }
  } catch { /* ignore */ }
  if (!S.tabs.find((t) => t.kind === "home")) S.tabs.unshift({ key: "home", kind: "home", title: "Mission control" });
  if (!S.tabs.find((t) => t.key === S.active)) S.active = "home";
  renderTabs(); renderStage();
  // live refresh: fast while anything runs, slow otherwise
  const poll = async () => {
    try { await loadSessions(); if (Math.random() < 0.3) loadStats(); } catch { /* offline */ }
    setTimeout(poll, S.sessions.some((s) => s.status === "running") ? 4000 : 20000);
  };
  setTimeout(poll, 4000);
})();
