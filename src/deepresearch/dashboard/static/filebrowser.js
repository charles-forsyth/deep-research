/* File browser for adding data sources (SPEC section 13.8).
 *
 * Places come from what is signed in on this machine: the allowed local folders,
 * each rclone Google Drive remote, Google Cloud Storage (gcloud), each rclone S3
 * remote (CephRDS). Browse, preview, tick files or open a folder, then add them as
 * one source. Loaded after sources.js (uses SRC) and before app.js; MODAL, api, esc,
 * busy and toast are globals from app.js, used only when the dialog opens.
 */
"use strict";

const FB = {
  st: null,
  kindIcon: { local: "\u{1F4BB}", drive: "\u{1F4C4}", gcs: "\u2601\uFE0F", s3: "\u{1FAA3}" },

  // open(opts): opts.onAdded(source) runs after a source is saved (e.g. file it in a project)
  async open(opts = {}) {
    this.st = { place: null, path: "", q: "", trail: [], items: [], picked: new Map(), opts, places: [], gen: 0 };
    const m = MODAL.open(`<h3>Add a data source <button class="fb-x" id="fb-x" aria-label="Close">\u00d7</button></h3>
      <div class="fb">
        <div class="fb-places" id="fb-places" role="tablist" aria-label="Places"><div class="dim">Loading places\u2026</div></div>
        <div class="fb-main">
          <div class="fb-bar"><div class="fb-crumbs" id="fb-crumbs"></div>
            <input id="fb-q" type="search" placeholder="Search Drive by name or text" autocomplete="off" hidden>
            <button class="btn small" id="fb-up" title="Up one level">\u2191 Up</button></div>
          <div class="fb-note dim" id="fb-note" role="status"></div>
          <div class="fb-split">
            <div class="fb-list" id="fb-list" role="listbox" aria-multiselectable="true" aria-label="Files and folders"></div>
            <div class="fb-prev" id="fb-prev"><div class="dim">Select a file to preview it. Tick the files you want, or open a folder and use \u201cAdd folder\u201d.</div></div>
          </div>
        </div>
      </div>
      <div class="fb-foot">
        <div class="fb-picked dim" id="fb-picked">Nothing picked.</div>
        <button class="btn small" id="fb-manual" title="Type a URL, bucket or path instead">Type a location\u2026</button>
        <button class="btn small" id="fb-addfolder" disabled>Add this folder</button>
        <button class="btn primary small" id="fb-addpicked" disabled>Add picked</button>
      </div>
      <div id="fb-name" hidden></div>`, { cls: "wide fb-modal", label: "Add a data source" });
    $("#fb-up", m).onclick = () => this.up();
    $("#fb-x", m).onclick = () => MODAL.requestClose();
    $("#fb-manual", m).onclick = () => { MODAL.close(); if (opts.manual) opts.manual(); };
    if (!opts.manual) $("#fb-manual", m).hidden = true;
    $("#fb-addfolder", m).onclick = () => this.prepare([this.folderItem()]);
    $("#fb-addpicked", m).onclick = () => this.prepare([...this.st.picked.values()]);
    let t = null;
    $("#fb-q", m).oninput = (e) => { clearTimeout(t); t = setTimeout(() => { this.st.q = e.target.value.trim(); this.st.q.length === 1 || this.go(this.st.place, this.st.q ? "" : this.st.path, true); }, 400); };
    try {
      const d = await api("/api/browse/places");
      this.st.places = d.places;
    } catch (e) { $("#fb-places", m).innerHTML = `<div class="err-banner">${esc(e.message)}</div>`; return; }
    this.drawPlaces();
    const last = localStorage.getItem("dr.fb.place");
    const first = this.st.places.find((p) => p.id === last) || this.st.places.find((p) => p.kind === "drive" && !p.shared_drive) || this.st.places[0];
    if (first) this.pick(first.id);
  },

  drawPlaces() {
    const box = $("#fb-places");
    box.innerHTML = this.st.places.map((p) => `<button class="fb-place${p.id === this.st.place ? " on" : ""}" role="tab" aria-selected="${p.id === this.st.place}" data-place="${esc(p.id)}" title="${esc(p.detail)}">
      <span class="fb-pi">${this.kindIcon[p.kind] || ""}</span><span class="fb-pl"><b>${esc(p.label.replace(/^(Google Drive|S3): /, ""))}</b><span class="dim">${esc(p.kind === "drive" ? "Google Drive" : p.kind === "s3" ? "S3 bucket store" : p.detail)}</span></span></button>`).join("");
    $$(".fb-place", box).forEach((b) => (b.onclick = () => this.pick(b.dataset.place)));
  },

  pick(place) {
    const p = this.st.places.find((x) => x.id === place); if (!p) return;
    this.st.picked.clear(); this.st.trail = []; this.st.q = "";
    const q = $("#fb-q"); q.value = ""; q.hidden = p.kind !== "drive";
    this.drawPlaces();
    this.go(place, "", false);
  },

  // trail: [{path, name}] of folders opened below the place's top
  async go(place, path, isSearch = false, name = null) {
    const st = this.st; if (!st) return;
    const gen = ++st.gen;
    if (!isSearch) {
      if (!path) st.trail = [];
      else {
        const i = st.trail.findIndex((x) => x.path === path);
        if (i >= 0) st.trail = st.trail.slice(0, i + 1);
        else st.trail.push({ path, name: name || path.split("/").filter(Boolean).pop() || path });
      }
    }
    st.place = place; st.path = isSearch ? "" : path;
    this.drawCrumbs();
    const list = $("#fb-list"); list.innerHTML = `<div class="dim fb-loading"><span class="spinner"></span> Loading\u2026</div>`;
    $("#fb-note").textContent = "";
    this.updateFoot();
    let d;
    try {
      const qs = new URLSearchParams({ place, path: st.path, q: isSearch ? st.q : "" });
      d = await api(`/api/browse/list?${qs}`);
    } catch (e) {
      if (gen !== st.gen) return;
      list.innerHTML = `<div class="err-banner">${esc(e.message)}</div>`; return;
    }
    if (gen !== st.gen || !$("#fb-list")) return; // a newer click won, or the dialog closed
    localStorage.setItem("dr.fb.place", place); // reopen at the last place that worked
    st.items = d.items;
    $("#fb-note").textContent = d.note || "";
    this.drawList();
    this.updateFoot();
  },

  up() {
    const st = this.st;
    if (st.q) { st.q = ""; $("#fb-q").value = ""; return this.go(st.place, st.path); }
    if (!st.trail.length) return;
    const prev = st.trail[st.trail.length - 2];
    st.trail.pop();
    if (prev) { st.trail.pop(); this.go(st.place, prev.path, false, prev.name); } else this.go(st.place, "");
  },

  drawCrumbs() {
    const st = this.st;
    const place = st.places.find((p) => p.id === st.place);
    const parts = [{ path: "", name: place ? place.label : "" }, ...st.trail];
    const c = $("#fb-crumbs");
    c.innerHTML = st.q ? `<span>Search: <b>${esc(st.q)}</b></span>` : parts.map((p, i) => i === parts.length - 1 ? `<b>${esc(p.name)}</b>` : `<a href="#" data-i="${i}">${esc(p.name)}</a>`).join(' <span class="dim">/</span> ');
    $$("a[data-i]", c).forEach((a) => (a.onclick = (e) => { e.preventDefault(); const p = parts[+a.dataset.i]; if (!p.path) this.go(st.place, ""); else this.go(st.place, p.path, false, p.name); }));
    $("#fb-up").disabled = !st.q && !st.trail.length;
  },

  drawList() {
    const st = this.st, list = $("#fb-list");
    if (!st.items.length) { list.innerHTML = `<div class="dim fb-empty">${st.q ? "No matches." : "Empty folder."}</div>`; return; }
    list.innerHTML = `<div class="fb-head" aria-hidden="true"><span></span><span></span><span>Name</span><span>Size</span><span>Modified</span></div>` + st.items.map((it, i) => {
      const on = st.picked.has(this.key(it));
      const tick = it.addable ? `<input type="checkbox" class="fb-tick" data-i="${i}" ${on ? "checked" : ""} aria-label="Pick ${esc(it.name)}">` : `<span class="fb-tick-x" title="${esc(it.why || "open it to pick what is inside")}"></span>`;
      return `<div class="fb-row${it.dir ? " dir" : ""}${on ? " on" : ""}" role="option" aria-selected="${on}" data-i="${i}" tabindex="0" title="${esc(it.name)}${it.why ? " \u2014 " + esc(it.why) : ""}">
        ${tick}<span class="fb-ico">${it.dir ? "\u{1F4C1}" : this.fileIcon(it)}</span>
        <span class="fb-nm"><span class="fb-nt">${esc(it.name)}</span>${it.badge ? `<span class="fb-badge">${esc(it.badge)}</span>` : ""}</span>
        <span class="fb-sz dim">${it.dir ? "" : it.size ? SRC.size(it.size) : ""}</span>
        <span class="fb-dt dim">${esc(it.modified || "")}</span></div>`;
    }).join("");
    $$(".fb-row", list).forEach((row) => {
      const it = st.items[+row.dataset.i];
      const open = () => { if (it.dir) this.go(st.place, it.path, false, it.name); else this.preview(it, row); };
      row.onclick = (e) => { if (e.target.classList.contains("fb-tick")) return; open(); };
      row.ondblclick = () => { if (!it.dir && it.addable) this.toggle(it, true); };
      row.onkeydown = (e) => {
        if (e.key === "Enter") { e.preventDefault(); open(); }
        else if (e.key === " " && it.addable) { e.preventDefault(); this.toggle(it); }
        else if (e.key === "ArrowDown") { e.preventDefault(); row.nextElementSibling?.focus(); }
        else if (e.key === "ArrowUp") { e.preventDefault(); row.previousElementSibling?.focus(); }
        else if (e.key === "Backspace") { e.preventDefault(); this.up(); }
      };
    });
    $$(".fb-tick", list).forEach((cb) => (cb.onchange = () => this.toggle(st.items[+cb.dataset.i], cb.checked)));
  },

  fileIcon(it) {
    if (it.badge === "Google Doc") return "\u{1F4DD}";
    if (it.badge === "Google Sheet") return "\u{1F4CA}";
    if (it.badge === "Google Slides") return "\u{1F4FD}\uFE0F";
    const ext = (it.name.split(".").pop() || "").toLowerCase();
    if (["csv", "tsv", "xlsx", "parquet"].includes(ext)) return "\u{1F4CA}";
    if (["png", "jpg", "jpeg", "gif", "tif", "tiff", "svg"].includes(ext)) return "\u{1F5BC}\uFE0F";
    if (["mp3", "wav", "m4a", "flac"].includes(ext)) return "\u{1F3B5}";
    if (["pdf"].includes(ext)) return "\u{1F4D5}";
    return "\u{1F4C4}";
  },

  key(it) { return `${this.st.place}|${it.path}`; },

  toggle(it, force) {
    const st = this.st, k = this.key(it);
    const on = force === undefined ? !st.picked.has(k) : force;
    if (on) st.picked.set(k, { ...it, _parent: st.q ? "(search)" : st.path });
    else st.picked.delete(k);
    this.drawList();
    this.updateFoot();
  },

  folderItem() {
    const st = this.st, last = st.trail[st.trail.length - 1];
    return last ? { name: last.name, path: last.path, dir: true, addable: true } : null;
  },

  updateFoot() {
    const st = this.st; if (!$("#fb-picked")) return;
    const n = st.picked.size;
    const names = [...st.picked.values()].map((x) => x.name);
    $("#fb-picked").innerHTML = n ? `<b>${n} picked:</b> ${esc(names.slice(0, 3).join(", "))}${n > 3 ? ` and ${n - 3} more` : ""} <button class="linkbtn" id="fb-clear">clear</button>` : "Nothing picked.";
    const c = $("#fb-clear"); if (c) c.onclick = () => { st.picked.clear(); this.drawList(); this.updateFoot(); };
    $("#fb-addpicked").disabled = !n;
    $("#fb-addpicked").textContent = n ? `Add ${n} picked` : "Add picked";
    const f = this.folderItem();
    const place = st.places.find((p) => p.id === st.place);
    // top-level containers are not sources (My Drive root, a GCS project, S3 remote root)
    const okFolder = f && !st.q && !(place && place.kind === "gcs" && f.path.startsWith("project:")) && !(place && place.kind === "drive" && ["root", "shared", "drives"].includes(f.path));
    $("#fb-addfolder").disabled = !okFolder;
    $("#fb-addfolder").textContent = okFolder ? `Add folder \u201c${f.name.length > 28 ? f.name.slice(0, 27) + "\u2026" : f.name}\u201d` : "Add this folder";
  },

  async preview(it, row) {
    $$(".fb-row.cur", $("#fb-list")).forEach((x) => x.classList.remove("cur")); row.classList.add("cur");
    const box = $("#fb-prev");
    const head = `<div class="fb-ph"><b>${esc(it.name)}</b>${it.badge ? ` <span class="fb-badge">${esc(it.badge)}</span>` : ""}<div class="dim">${it.size ? SRC.size(it.size) + " \u00b7 " : ""}${esc(it.modified || "")}${it.link && /^https:\/\//.test(it.link) ? ` \u00b7 <a href="${esc(it.link)}" target="_blank" rel="noopener noreferrer">open in Drive \u2197</a>` : ""}</div>
      ${it.addable ? `<button class="btn small" id="fb-pv-pick">${this.st.picked.has(this.key(it)) ? "Unpick" : "Pick this"}</button>` : `<div class="dim">${esc(it.why || "")}</div>`}</div>`;
    box.innerHTML = head + `<div class="dim"><span class="spinner"></span> Reading\u2026</div>`;
    const wire = () => { const b = $("#fb-pv-pick"); if (b) b.onclick = () => { this.toggle(it); b.textContent = this.st.picked.has(this.key(it)) ? "Unpick" : "Pick this"; }; };
    wire();
    if (!it.addable) { box.innerHTML = head; wire(); return; }
    const gen = this.st.gen;
    try {
      const qs = new URLSearchParams({ place: this.st.place, path: it.path, size: String(it.size || 0) });
      const p = await api(`/api/browse/preview?${qs}`);
      if (gen !== this.st.gen || !row.classList.contains("cur")) return;
      let body;
      if (p.binary) body = `<div class="dim">Binary file; no text preview.</div>`;
      else if (/\.(csv|tsv)$/i.test(it.name) || it.badge === "Google Sheet") {
        const sep = /\.tsv$/i.test(it.name) ? "\t" : ",";
        const rows = p.text.split("\n").slice(0, 30).filter(Boolean).map((l) => l.split(sep).slice(0, 12));
        body = `<div class="src-csv"><table>${rows.map((r, i) => `<tr>${r.map((c) => i ? `<td>${esc(c)}</td>` : `<th>${esc(c)}</th>`).join("")}</tr>`).join("")}</table></div>`;
      } else if (it.badge === "Google Doc" || /\.md$/i.test(it.name)) {
        // a Doc usually starts with its own title; do not show it twice
        let md = p.text.slice(0, 20000);
        const first = md.match(/^\s*#\s+\**(.+?)\**\s*\n/);
        if (first && first[1].trim().replace(/\\/g, "") === it.name.replace(/\.md$/i, "").trim()) md = md.slice(first[0].length);
        body = `<div class="fb-md md">${DOMPurify.sanitize(marked.parse(md))}</div>`;
      } else body = `<pre class="src-pre">${esc(p.text)}</pre>`;
      box.innerHTML = head + body;
    } catch (e) {
      if (gen !== this.st.gen) return;
      box.innerHTML = head + `<div class="err-banner">${esc(e.message)}</div>`;
    }
    wire();
  },

  // turn the picked items into a source spec, then ask for a name
  async prepare(items) {
    items = items.filter(Boolean); if (!items.length) return;
    const st = this.st;
    const parents = new Set(items.map((x) => x._parent ?? st.path));
    const place = st.places.find((p) => p.id === st.place);
    if (place.kind !== "drive" && parents.size > 1) { toast("Pick files from one folder at a time (or add their common folder).", "err"); return; }
    let spec;
    try { spec = await api("/api/browse/spec", { method: "POST", body: { place: st.place, items } }); } catch (e) { toast(e.message, "err"); return; }
    const f = $("#fb-name"); f.hidden = false;
    const what = spec.kind === "gdrive" ? (spec.uri.startsWith("gdrive://folder/") ? "Drive folder (Docs as Markdown, Sheets as CSV)" : `${items.length} Drive file${items.length === 1 ? "" : "s"} (Docs as Markdown, Sheets as CSV)`) : `${SRC.kindLabel[spec.kind] || spec.kind}: ${spec.uri}${spec.options.include ? ` (${spec.options.include.length} picked)` : ""}`;
    f.innerHTML = `<div class="fb-namebox">
      <div class="dim">${esc(what)}</div>
      <div class="src-grid">
        <label>Name<input id="fb-n" value="${esc(spec.name)}" autocomplete="off"></label>
        <label>Title<input id="fb-t" value="${esc(spec.title || "")}" autocomplete="off"></label>
        <label>Level<select id="fb-l">${["P1", "P2", "P3", "P4"].map((x) => `<option ${x === "P2" ? "selected" : ""}>${x}</option>`).join("")}</select></label>
      </div>
      <div class="src-actions"><button class="btn primary small" id="fb-save">List and add</button><button class="btn small" id="fb-back">Back</button><span class="dim" id="fb-msg" role="status"></span></div></div>`;
    $("#fb-n", f).focus(); $("#fb-n", f).select();
    $("#fb-back", f).onclick = () => { f.hidden = true; };
    f.onkeydown = (e) => { if (e.key === "Enter" && e.target.tagName === "INPUT") { e.preventDefault(); $("#fb-save", f).click(); } };
    const save = $("#fb-save", f);
    save.onclick = () => busy(save, async () => {
      const name = $("#fb-n", f).value.trim();
      if (!/^[a-z0-9][a-z0-9-]{1,40}$/.test(name)) { $("#fb-msg", f).textContent = "Name: 2-41 lowercase letters, digits or dashes"; return; }
      $("#fb-msg", f).textContent = "listing\u2026";
      try {
        const s = await api("/api/sources", { method: "POST", body: { name, title: $("#fb-t", f).value.trim(), uri: spec.uri, kind: spec.kind, auth_ref: spec.auth_ref, options: spec.options, protection_level: $("#fb-l", f).value } });
        toast(s.status === "ok" ? `Added ${s.name} (${s.manifest ? s.manifest.file_count + (s.manifest.truncated ? "+" : "") : 0} files)` : `Saved ${s.name}, but: ${s.last_error}`, s.status === "ok" ? "ok" : "err");
        const cb = st.opts.onAdded;
        MODAL.close();
        SRC.load().catch(() => {});
        if (cb) await cb(s);
      } catch (e) { $("#fb-msg", f).textContent = e.message; throw e; }
    });
  },
};
