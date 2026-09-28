/* Data sources: the library view (add, test, browse, preview) and a picker the launcher,
   Ask and Lab use. Loaded before app.js; uses app.js helpers (api, esc, toast) at call time. */
"use strict";

const SRC = {
  list: [],
  roots: [],

  kindLabel: { web: "Web", gcs: "GCS", s3: "S3 / Ceph", local_folder: "Folder", local_file: "File", report: "Report", notebook: "Notebook" },

  size(n) {
    if (!n) return "0 B";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + " " + u[i];
  },

  async load() {
    const d = await api("/api/sources");
    this.list = d.sources || []; this.roots = d.local_roots || [];
    return this.list;
  },

  // ------------------------------------------------------------------ library tab
  async render(v) {
    v.innerHTML = `<div class="src-view">
      <div class="src-head"><h2>Data sources</h2>
        <span class="dim">Places your data lives. Use them in research, follow-ups and Lab runs.</span>
        <button class="btn primary small" id="src-add">+ Add source</button>
        <button class="btn small" id="src-reload">Refresh</button></div>
      <div id="src-form" hidden></div>
      <div id="src-body"><div class="dim">Loading\u2026</div></div></div>`;
    $("#src-add", v).onclick = () => this.form(v);
    $("#src-reload", v).onclick = () => this.render(v);
    try { await this.load(); } catch (e) { $("#src-body", v).innerHTML = `<div class="err-banner">${esc(e.message)}</div>`; return; }
    const body = $("#src-body", v);
    if (!this.list.length) {
      body.innerHTML = `<div class="empty">No sources yet. Add a web dataset, a gs:// or s3:// bucket, or a folder under ${esc(this.roots.join(", "))}.</div>`;
      return;
    }
    body.innerHTML = `<table class="src-table"><thead><tr><th>Name</th><th>Kind</th><th>Status</th><th>Files</th><th>Size</th><th>Lab staging</th><th>Level</th><th>Location</th></tr></thead><tbody>${this.list.map((s) => {
      const m = s.manifest;
      return `<tr data-id="${s.id}"><td><b>${esc(s.name)}</b>${s.title ? `<div class="dim">${esc(s.title)}</div>` : ""}</td>
        <td>${esc(this.kindLabel[s.kind] || s.kind)}</td>
        <td><span class="chip ${s.status === "ok" ? "ok" : s.status === "unreachable" ? "bad" : ""}"><span class="dot"></span>${esc(s.status)}</span></td>
        <td>${m ? m.file_count + (m.truncated ? "+" : "") : "-"}</td><td>${m ? this.size(m.total_bytes) : "-"}</td>
        <td>${esc(s.effective_staging)}</td><td>${esc(s.protection_level)}</td>
        <td class="mono src-uri" title="${esc(s.uri)}">${esc(s.uri)}</td></tr>`;
    }).join("")}</tbody></table>`;
    $$("tr[data-id]", body).forEach((tr) => (tr.onclick = () => openTab({ key: `src${tr.dataset.id}`, kind: "source", id: Number(tr.dataset.id), title: this.list.find((s) => s.id == tr.dataset.id)?.name || "Source" })));
  },

  form(v) {
    const f = $("#src-form", v); f.hidden = false;
    f.innerHTML = `<div class="src-card">
      <div class="src-grid">
        <label>Name<input id="sf-name" placeholder="noaa-ghcn" autocomplete="off"></label>
        <label>Location<input id="sf-uri" placeholder="https://..., gs://bucket/prefix, s3://bucket/prefix, or ~/data/folder" autocomplete="off"></label>
        <label>Kind<select id="sf-kind"><option value="">auto</option>${Object.entries(this.kindLabel).map(([k, l]) => `<option value="${k}">${l}</option>`).join("")}</select></label>
        <label>Credentials<input id="sf-auth" placeholder="S3/Ceph: rclone:ceph" autocomplete="off"></label>
        <label>Lab staging<select id="sf-staging"><option value="auto">auto</option><option value="relay">relay (this machine uploads)</option><option value="direct">direct (cluster downloads)</option></select></label>
        <label>Level<select id="sf-level"><option value="">default</option><option>P1</option><option>P2</option><option>P3</option><option>P4</option></select></label>
        <label class="wide">Title<input id="sf-title" autocomplete="off"></label>
        <label class="wide">Only files matching (globs, comma separated)<input id="sf-include" placeholder="*.csv, *.parquet" autocomplete="off"></label>
      </div>
      <div class="src-actions"><button class="btn primary small" id="sf-save">Test and save</button><button class="btn small" id="sf-cancel">Cancel</button><span class="dim" id="sf-status"></span></div>
      <div class="dim">Local sources must be under ${esc(this.roots.join(", "))}. Ceph needs the campus VPN on this machine; Lab jobs get Ceph data by relay.</div></div>`;
    $("#sf-cancel", f).onclick = () => { f.hidden = true; };
    $("#sf-save", f).onclick = async () => {
      const inc = $("#sf-include", f).value.split(",").map((x) => x.trim()).filter(Boolean);
      const body = { name: $("#sf-name", f).value.trim(), uri: $("#sf-uri", f).value.trim(), kind: $("#sf-kind", f).value || undefined,
        auth_ref: $("#sf-auth", f).value.trim(), staging: $("#sf-staging", f).value, title: $("#sf-title", f).value.trim(),
        options: inc.length ? { include: inc } : {} };
      if ($("#sf-level", f).value) body.protection_level = $("#sf-level", f).value;
      $("#sf-status", f).textContent = "testing\u2026"; $("#sf-save", f).disabled = true;
      try {
        const s = await api("/api/sources", { method: "POST", body });
        toast(s.status === "ok" ? `Source ${s.name} added (${s.manifest?.file_count ?? 0} files)` : `Saved ${s.name}, but it is unreachable: ${s.last_error}`, s.status === "ok" ? "ok" : "err");
        this.render(v);
      } catch (e) { $("#sf-status", f).textContent = e.message; $("#sf-save", f).disabled = false; }
    };
  },

  // ------------------------------------------------------------------ detail tab
  async renderOne(v, id) {
    v.innerHTML = `<div class="src-view"><div class="dim">Loading\u2026</div></div>`;
    let s;
    try { s = await api(`/api/sources/${id}`); } catch (e) { v.innerHTML = `<div class="err-banner">${esc(e.message)}</div>`; return; }
    const m = s.manifest;
    v.innerHTML = `<div class="src-view">
      <div class="src-head"><h2>${esc(s.name)}</h2><span class="chip ${s.status === "ok" ? "ok" : s.status === "unreachable" ? "bad" : ""}"><span class="dot"></span>${esc(s.status)}</span>
        <button class="btn small" id="sd-test">Test now</button><button class="btn small danger" id="sd-del">Delete</button></div>
      ${s.last_error ? `<div class="err-banner">${esc(s.last_error)}</div>` : ""}
      <div class="src-meta">
        <div><span class="dim">Kind</span><span>${esc(this.kindLabel[s.kind] || s.kind)}</span></div>
        <div><span class="dim">Location</span><span><span class="mono">${esc(s.uri)}</span></span></div>
        <div><span class="dim">Files</span><span>${m ? m.file_count + (m.truncated ? "+" : "") + " (" + this.size(m.total_bytes) + ")" : "not listed yet"}</span></div>
        <div><span class="dim">Formats</span><span>${m ? Object.entries(m.formats).slice(0, 8).map(([k, n]) => `${esc(k)} ${n}`).join(", ") : "-"}</span></div>
        <div><span class="dim">Lab staging</span><span>${esc(s.effective_staging)}; jobs read <span class="mono">$${esc(s.env_var)}</span></span></div>
        <div><span class="dim">Level</span><span>${esc(s.protection_level)} (label only)</span></div>
        ${s.auth_ref ? `<div><span class="dim">Credentials</span><span><span class="mono">${esc(s.auth_ref)}</span></span></div>` : ""}
        <div><span class="dim">Checked</span><span>${esc(s.last_checked || "never")}</span></div>
        <div><span class="dim">Used by</span><span>${s.used_by.length ? s.used_by.slice(0, 12).map((u) => `${esc(u.used_by_kind)} #${u.used_by_id}`).join(", ") : "nothing yet"}</span></div>
      </div>
      <div class="src-split"><div class="src-tree"><div class="src-crumbs" id="sd-crumbs"></div><div id="sd-items"></div></div>
        <div class="src-preview" id="sd-prev"><div class="dim">Pick a file to preview it.</div></div></div></div>`;
    $("#sd-test", v).onclick = async () => { $("#sd-test", v).disabled = true; try { const r = await api(`/api/sources/${id}/test`, { method: "POST" }); toast(r.status === "ok" ? "Reachable" : r.last_error, r.status === "ok" ? "ok" : "err"); } catch (e) { toast(e.message, "err"); } this.renderOne(v, id); };
    $("#sd-del", v).onclick = async () => { if (!confirm(`Delete source ${s.name}? The data itself is not touched.`)) return; await api(`/api/sources/${id}`, { method: "DELETE" }); toast("Deleted", "ok"); closeTab(`src${id}`); };
    this.browse(v, id, "");
  },

  async browse(v, id, path) {
    const crumbs = ["", ...path.split("/").filter(Boolean)];
    $("#sd-crumbs", v).innerHTML = crumbs.map((c, i) => `<a data-p="${esc(crumbs.slice(1, i + 1).join("/"))}">${i ? esc(c) : "root"}</a>`).join(" / ");
    $$("#sd-crumbs a", v).forEach((a) => (a.onclick = () => this.browse(v, id, a.dataset.p)));
    const box = $("#sd-items", v); box.innerHTML = `<div class="dim">\u2026</div>`;
    let d;
    try { d = await api(`/api/sources/${id}/browse?path=${encodeURIComponent(path)}`); } catch (e) { box.innerHTML = `<div class="err-banner">${esc(e.message)}</div>`; return; }
    if (!d.items.length) { box.innerHTML = `<div class="dim">Empty.</div>`; return; }
    box.innerHTML = d.items.map((it) => `<div class="src-item ${it.dir ? "dir" : ""}" data-n="${esc(it.name)}" title="${esc(it.name)}"><span>${it.dir ? "\u{1F4C1} " : ""}${esc(it.name)}</span><span class="dim">${this.size(it.size)}</span></div>`).join("");
    $$(".src-item", box).forEach((el) => (el.onclick = () => {
      const n = el.dataset.n, full = (path ? path + "/" : "") + n.replace(/\/$/, "");
      if (el.classList.contains("dir")) this.browse(v, id, full); else this.preview(v, id, full);
    }));
  },

  async preview(v, id, path) {
    const box = $("#sd-prev", v); box.innerHTML = `<div class="dim">Loading ${esc(path)}\u2026</div>`;
    try {
      const p = await api(`/api/sources/${id}/preview?path=${encodeURIComponent(path)}`);
      if (p.binary) { box.innerHTML = `<div class="dim">${esc(path)} is a binary file (${this.size(p.bytes)} read); no text preview.</div>`; return; }
      const lines = p.text.split("\n");
      const csv = /\.(csv|tsv)$/i.test(path);
      if (csv) {
        const sep = /\.tsv$/i.test(path) ? "\t" : ",";
        const rows = lines.slice(0, 40).filter(Boolean).map((l) => l.split(sep));
        box.innerHTML = `<div class="dim">${esc(path)} (first rows)</div><div class="src-csv"><table>${rows.map((r, i) => `<tr>${r.map((c) => i ? `<td>${esc(c)}</td>` : `<th>${esc(c)}</th>`).join("")}</tr>`).join("")}</table></div>`;
      } else {
        box.innerHTML = `<div class="dim">${esc(path)} (first ${this.size(p.bytes)})</div><pre class="src-pre">${esc(p.text)}</pre>`;
      }
    } catch (e) { box.innerHTML = `<div class="err-banner">${esc(e.message)}</div>`; }
  },

  // ------------------------------------------------------------------ picker
  // mount(el, selected[]) renders chips + a dropdown; returns a getter for the names.
  picker(el, selected = [], opts = {}) {
    const chosen = new Set(selected);
    const draw = () => {
      if (opts.compact && !this.list.length) { el.hidden = true; return; }
      el.innerHTML = `<div class="src-picker${opts.compact ? " compact" : ""}">${opts.compact ? '<span class="dim">Include data:</span>' : ""}${[...chosen].map((n) => `<span class="filechip">${esc(n)}<button data-rm="${esc(n)}">\u00d7</button></span>`).join("")}
        <select class="src-pick"><option value="">+ add a data source</option>${this.list.filter((s) => !chosen.has(s.name)).map((s) => `<option value="${esc(s.name)}">${esc(s.name)} (${esc(this.kindLabel[s.kind] || s.kind)}${s.status !== "ok" ? ", " + esc(s.status) : ""})</option>`).join("")}</select></div>`;
      $$("[data-rm]", el).forEach((b) => (b.onclick = () => { chosen.delete(b.dataset.rm); draw(); }));
      $(".src-pick", el).onchange = (e) => { if (e.target.value) { chosen.add(e.target.value); draw(); } };
    };
    (this.list.length ? Promise.resolve() : this.load()).then(draw).catch(() => { el.innerHTML = `<span class="dim">Sources unavailable</span>`; });
    return () => [...chosen];
  },
};
