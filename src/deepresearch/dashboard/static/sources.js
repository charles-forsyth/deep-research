/* Data sources: the library view (add, test, browse, preview) and a picker the launcher,
   Ask and Lab use. Loaded before app.js; uses app.js helpers (api, esc, toast) at call time. */
"use strict";

const SRC = {
  list: [],
  roots: [],

  kindLabel: { web: "Web", gcs: "GCS", s3: "S3 / Ceph", public_bucket: "Public bucket", local_folder: "Folder", local_file: "File", report: "Report", notebook: "Notebook" },

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
        <button class="btn small" id="src-find">Find open datasets</button>
        <button class="btn small" id="src-reload">Refresh</button></div>
      <div id="src-form" hidden></div>
      <div id="src-disc" hidden></div>
      <div id="src-body"><div class="dim">Loading\u2026</div></div></div>`;
    $("#src-add", v).onclick = () => this.form(v);
    $("#src-find", v).onclick = () => this.discover(v);
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

  discover(v) {
    const d = $("#src-disc", v); d.hidden = false;
    d.innerHTML = `<div class="src-card">
      <div class="src-disc-bar"><input id="sd-q" placeholder="Search Data.gov, Zenodo, Hugging Face, AWS Open Data, Google Cloud, Earth Engine" autocomplete="off"><button class="btn primary small" id="sd-go">Search</button><button class="btn small" id="sd-close">Close</button></div>
      <div class="dim" style="font-size:11.5px">Free sources only. Nothing is downloaded while you search. Check each dataset's license before you use it.</div>
      <div id="sd-res"></div></div>`;
    $("#sd-close", d).onclick = () => { d.hidden = true; };
    const run = async () => {
      const q = $("#sd-q", d).value.trim(); if (q.length < 2) return;
      const box = $("#sd-res", d); box.innerHTML = '<span class="spinner"></span> searching\u2026';
      let out;
      try { out = await api(`/api/sources/discover?q=${encodeURIComponent(q)}`); } catch (e) { box.innerHTML = `<div class="err-banner">${esc(e.message)}</div>`; return; }
      const errs = Object.entries(out.errors || {}).map(([c, e]) => `<div class="dim" style="font-size:11px">${esc(c)} unavailable: ${esc(clip(e, 120))}</div>`).join("");
      box.innerHTML = errs + (out.results.length ? out.results.map((r, i) => `<div class="src-hit">
        <div class="src-hit-h"><b>${esc(r.title)}</b> <span class="chip">${esc({ datagov: "Data.gov", zenodo: "Zenodo", huggingface: "Hugging Face", aws: "AWS Open Data", gcp: "Google Cloud", earthengine: "Earth Engine" }[r.catalog] || r.catalog)}</span> <span class="dim src-hit-lic">${esc(r.license || "license not stated")}${r.publisher ? " \u00b7 " + esc(clip(r.publisher, 60)) : ""}</span></div>
        ${r.description ? `<div class="dim" style="font-size:12px">${esc(clip(r.description, 260))}</div>` : ""}
        <div style="font-size:11.5px">${r.page ? `<a class="src-hit-page" href="${esc(r.page)}" target="_blank" rel="noopener">dataset page \u2197</a>` : ""}</div>
        ${(r.buckets || []).length ? `<div class="src-hit-files">${r.buckets.slice(0, 5).map((b, j) => `<div><span class="mono dim">bucket</span> <span class="mono src-uri" title="${esc(b.uri + (b.title ? " \u2014 " + b.title : ""))}">${esc(b.uri)}</span> <button class="linkbtn" data-addb="${i}:${j}" title="Read anonymously over HTTPS: free, no account">add as source</button></div>`).join("")}<div class="dim" style="font-size:10.5px">Public bucket: read anonymously, free. Big buckets: add a narrower prefix or file filter.</div></div>` : ""}
        ${r.note ? `<div class="dim" style="font-size:11px">${esc(r.note)}</div>` : ""}
        ${r.files.length ? `<div class="src-hit-files">${r.files.slice(0, 5).map((f, j) => `<div><span class="mono dim">${esc(f.format || "?")}</span> <span class="mono src-uri" title="${esc(f.url)}">${esc(f.title || f.url)}</span> <button class="linkbtn" data-add="${i}:${j}">add as source</button></div>`).join("")}</div>` : (r.buckets || []).length || r.note ? "" : '<div class="dim" style="font-size:11px">No direct download links; open the dataset page.</div>'}
      </div>`).join("") : '<div class="dim">No datasets found.</div>');
      $$("[data-addb]", box).forEach((b) => (b.onclick = async () => {
        const [i, j] = b.dataset.addb.split(":").map(Number);
        const r = out.results[i], bk = r.buckets[j];
        const tail = bk.uri.replace(/^(s3|gs):\/\//, "").split("/").filter(Boolean).pop() || "bucket";
        const name = prompt("Name for this source", tail.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40).replace(/-+$/, "") || "bucket"); if (!name) return;
        const sub = prompt("Only a part of the bucket? Add a sub-folder (optional), e.g. csv/by_year", "");
        if (sub === null) return;
        const inc = prompt("Only files matching (optional, comma separated), e.g. *.csv", "");
        if (inc === null) return;
        const uri = sub.trim() ? bk.uri.replace(/\/+$/, "") + "/" + sub.trim().replace(/^\/+|\/+$/g, "") : bk.uri;
        const include = inc.split(",").map((x) => x.trim()).filter(Boolean);
        b.disabled = true; b.textContent = "listing\u2026";
        try {
          const s = await api("/api/sources", { method: "POST", body: { name, uri, kind: "public_bucket", title: clip(r.title, 120), description: `${r.page || ""} (license: ${clip(r.license || "not stated", 100)})`.trim(), tags: [r.catalog], options: { ...(include.length ? { include } : {}), ...(bk.region ? { region: bk.region } : {}) } } });
          toast(s.status === "ok" ? `Added ${s.name} (${s.manifest?.file_count ?? 0}${s.manifest?.truncated ? "+" : ""} files)` : `Saved ${s.name}, but: ${s.last_error}`, s.status === "ok" ? "ok" : "err");
          b.textContent = "added";
        } catch (e) { toast(e.message, "err"); b.disabled = false; b.textContent = "add as source"; }
      }));
      $$("[data-add]", box).forEach((b) => (b.onclick = async () => {
        const [i, j] = b.dataset.add.split(":").map(Number);
        const r = out.results[i], f = r.files[j];
        const base = (f.title || r.title || "dataset").toLowerCase().replace(/\.[a-z0-9]+$/, "").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40).replace(/-+$/, "") || "dataset";
        const name = prompt("Name for this source", base); if (!name) return;
        b.disabled = true; b.textContent = "testing\u2026";
        try {
          const s = await api("/api/sources", { method: "POST", body: { name, uri: f.url, kind: "web", title: clip(r.title, 120), description: `${r.page || ""} (license: ${r.license || "not stated"})`.trim(), tags: [r.catalog] } });
          toast(s.status === "ok" ? `Added ${s.name}` : `Saved ${s.name}, but it is unreachable: ${s.last_error}`, s.status === "ok" ? "ok" : "err");
          b.textContent = "added";
        } catch (e) { toast(e.message, "err"); b.disabled = false; b.textContent = "add as source"; }
      }));
    };
    $("#sd-go", d).onclick = run;
    $("#sd-q", d).onkeydown = (e) => { if (e.key === "Enter") run(); };
    $("#sd-q", d).focus();
  },

  form(v) {
    const f = $("#src-form", v); f.hidden = false;
    f.innerHTML = `<div class="src-card">
      <div class="src-grid">
        <label>Name<input id="sf-name" placeholder="noaa-ghcn" autocomplete="off"></label>
        <label>Location<input id="sf-uri" placeholder="https://..., gs://bucket/prefix, s3://bucket/prefix, or ~/data/folder" autocomplete="off"></label>
        <label>Kind<select id="sf-kind"><option value="">auto</option>${Object.entries(this.kindLabel).map(([k, l]) => `<option value="${k}">${l}</option>`).join("")}</select></label>
        <label>Credentials<input id="sf-auth" placeholder="blank = public bucket (free, anonymous); Ceph: rclone:ceph" autocomplete="off"></label>
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
        <div><span class="dim">Search index</span><span>${{ none: "none (research runs upload its files each time)", current: "saved and current", stale: "saved, but the source changed; rebuilt on next use" }[s.index_state] || "-"}
          <button class="linkbtn" id="sd-index">${s.index_state === "none" ? "build" : "rebuild"}</button>${s.index_state !== "none" ? ' <button class="linkbtn" id="sd-unindex">delete</button>' : ""}</span></div>
        <div><span class="dim">Used by</span><span>${s.used_by.length ? s.used_by.slice(0, 12).map((u) => `${esc(u.used_by_kind)} #${u.used_by_id}`).join(", ") : "nothing yet"}</span></div>
      </div>
      <div class="src-split"><div class="src-tree"><div class="src-crumbs" id="sd-crumbs"></div><div id="sd-items"></div></div>
        <div class="src-preview" id="sd-prev"><div class="dim">Pick a file to preview it.</div></div></div></div>`;
    $("#sd-test", v).onclick = async () => { $("#sd-test", v).disabled = true; try { const r = await api(`/api/sources/${id}/test`, { method: "POST" }); toast(r.status === "ok" ? "Reachable" : r.last_error, r.status === "ok" ? "ok" : "err"); } catch (e) { toast(e.message, "err"); } this.renderOne(v, id); };
    $("#sd-del", v).onclick = async () => { if (!confirm(`Delete source ${s.name}? The data itself is not touched.`)) return; await api(`/api/sources/${id}`, { method: "DELETE" }); toast("Deleted", "ok"); closeTab(`src${id}`); };
    $("#sd-index", v).onclick = async () => {
      const b = $("#sd-index", v); b.disabled = true; b.textContent = "building\u2026";
      try { await api(`/api/sources/${id}/index`, { method: "POST" }); toast("Index saved; research runs will reuse it", "ok"); } catch (e) { toast(e.message, "err"); }
      this.renderOne(v, id);
    };
    const un = $("#sd-unindex", v);
    if (un) un.onclick = async () => { try { await api(`/api/sources/${id}/index`, { method: "DELETE" }); toast("Index deleted", "ok"); } catch (e) { toast(e.message, "err"); } this.renderOne(v, id); };
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
