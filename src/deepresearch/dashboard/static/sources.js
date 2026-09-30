/* Data sources: the library view (add, test, browse, preview) and a picker the launcher,
   Ask and Lab use. Loaded before app.js; uses app.js helpers (api, esc, toast) at call time. */
"use strict";

const SRC = {
  list: [],
  roots: [],

  kindLabel: { web: "Web", gcs: "GCS", s3: "S3 / Ceph", public_bucket: "Public bucket", local_folder: "Folder", local_file: "File", report: "Report", notebook: "Notebook", gdrive: "Google Drive" },

  size(n) {
    if (!n) return "0 B";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + " " + u[i];
  },

  // only http(s) links from outside catalogs become clickable (no javascript: etc.)
  httpUrl(u) { try { return ["http:", "https:"].includes(new URL(u).protocol); } catch { return false; } },
  // "application/vnd.ms-excel Excel" -> "Excel"; "text/csv" -> "CSV"
  fmtLabel(f) {
    const t = String(f || "").trim(); if (!t) return "?";
    const words = t.split(/\s+/).filter((w) => !w.includes("/") && !/^(vnd\.|x-|plain$|octet)/i.test(w) && !w.includes("+"));
    if (words.length) return words.join(" ");
    const sub = t.split("/").pop().replace(/^(vnd\.|x-)/, "").split(/[+.;]/)[0];
    return { plain: "Text", "ms-excel": "Excel", octet: "Binary" }[sub] || sub.toUpperCase();
  },
  // re-draw the library table in place (after an add from discovery)
  refreshTable(v) { if (v && v.isConnected && $("#src-body", v)) this.render(v, { keepPanels: true }); },

  async load() {
    const d = await api("/api/sources");
    this.list = d.sources || []; this.roots = d.local_roots || [];
    return this.list;
  },

  // ------------------------------------------------------------------ library tab
  async render(v, o = {}) {
    if (o.keepPanels && $("#src-body", v)) {
      try { await this.load(); } catch { return; }
      const tmp = document.createElement("div"); await this.render(tmp);
      $("#src-body", v).replaceWith($("#src-body", tmp));
      $$("#src-body tr[data-id]", v).forEach((tr) => {
        const open = () => openTab({ key: `src${tr.dataset.id}`, kind: "source", id: Number(tr.dataset.id), title: this.list.find((x) => x.id == tr.dataset.id)?.name || "Source" });
        tr.tabIndex = 0; tr.setAttribute("role", "link"); tr.onclick = open;
        tr.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
      });
      return;
    }
    v.innerHTML = `<div class="src-view">
      <div class="src-head"><h2>Data sources</h2>
        <span class="dim">Places your data lives. Use them in research, follow-ups and Lab runs.</span>
        <button class="btn primary small" id="src-add">+ Add source</button>
        <button class="btn small" id="src-add-manual" title="Type a URL, gs:// or s3:// address, or a path">Type a location</button>
        <button class="btn small" id="src-find">Find open datasets</button>
        <button class="btn small" id="src-reload">Refresh</button></div>
      <div id="src-form" hidden></div>
      <div id="src-disc" hidden></div>
      <div id="src-body"><div class="dim">Loading\u2026</div></div></div>`;
    // the file browser first; typing a location stays one click away
    $("#src-add", v).onclick = () => (typeof FB !== "undefined" ? FB.open({ manual: () => this.form(v), onAdded: (s) => { this.render(v); openTab({ key: `src${s.id}`, kind: "source", id: s.id, title: s.name }); } }) : this.form(v));
    $("#src-add-manual", v).onclick = () => this.form(v);
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
    $$("tr[data-id]", body).forEach((tr) => {
      const open = () => openTab({ key: `src${tr.dataset.id}`, kind: "source", id: Number(tr.dataset.id), title: this.list.find((s) => s.id == tr.dataset.id)?.name || "Source" });
      tr.tabIndex = 0; tr.setAttribute("role", "link");
      tr.onclick = open;
      tr.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
    });
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
      const CAT = { datagov: "Data.gov", zenodo: "Zenodo", huggingface: "Hugging Face", aws: "AWS Open Data", gcp: "Google Cloud", earthengine: "Earth Engine" };
      const counts = {}; out.results.forEach((r) => (counts[r.catalog] = (counts[r.catalog] || 0) + 1));
      const filt = Object.keys(counts).length > 1 ? `<div class="src-disc-filter" role="group" aria-label="Filter by catalog"><button class="linkbtn on" data-cat="">All ${out.results.length}</button>${Object.entries(counts).map(([c, n]) => `<button class="linkbtn" data-cat="${esc(c)}">${esc(CAT[c] || c)} ${n}</button>`).join("")}</div>` : "";
      box.innerHTML = errs + filt + (out.results.length ? out.results.map((r, i) => `<div class="src-hit" data-cat="${esc(r.catalog)}">
        <div class="src-hit-h"><b>${esc(r.title)}</b> <span class="chip">${esc(CAT[r.catalog] || r.catalog)}</span> <span class="dim src-hit-lic">${esc(r.license || "license not stated")}${r.publisher ? " \u00b7 " + esc(clip(r.publisher, 60)) : ""}</span></div>
        ${r.description ? `<div class="dim" style="font-size:12px">${esc(clip(r.description, 260))}</div>` : ""}
        <div style="font-size:11.5px">${this.httpUrl(r.page) ? `<a class="src-hit-page" href="${esc(r.page)}" target="_blank" rel="noopener noreferrer">dataset page \u2197</a>` : ""}</div>
        ${(r.buckets || []).length ? `<div class="src-hit-files">${r.buckets.slice(0, 5).map((b, j) => `<div><span class="mono dim">bucket</span> <span class="mono src-uri" title="${esc(b.uri + (b.title ? " \u2014 " + b.title : ""))}">${esc(b.uri)}</span> <button class="linkbtn" data-addb="${i}:${j}" title="Read anonymously over HTTPS: free, no account">add as source</button></div>`).join("")}<div class="dim" style="font-size:10.5px">Public bucket: read anonymously, free. Big buckets: add a narrower prefix or file filter.</div></div>` : ""}
        ${r.note ? `<div class="dim" style="font-size:11px">${esc(r.note)}</div>` : ""}
        ${r.files.length ? `<div class="src-hit-files">${r.files.slice(0, 5).map((f, j) => `<div><span class="mono dim">${esc(this.fmtLabel(f.format))}</span> <span class="mono src-uri" title="${esc(f.url)}">${esc(f.title || f.url)}</span> <button class="linkbtn" data-add="${i}:${j}">add as source</button></div>`).join("")}</div>` : (r.buckets || []).length || r.note ? "" : '<div class="dim" style="font-size:11px">No direct download links; open the dataset page.</div>'}
      </div>`).join("") : '<div class="dim">No datasets found.</div>');
      $$("[data-cat]", box).forEach((b) => { if (b.tagName !== "BUTTON") return; b.onclick = () => {
        $$(".src-disc-filter .linkbtn", box).forEach((x) => x.classList.toggle("on", x === b));
        $$(".src-hit", box).forEach((h) => (h.hidden = !!b.dataset.cat && h.dataset.cat !== b.dataset.cat));
      }; });
      const slugName = (t, fb) => (t || "").toLowerCase().replace(/\.[a-z0-9]+$/, "").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40).replace(/-+$/, "") || fb;
      // One inline form under the result (name, and for buckets a sub-folder and a
      // file filter) instead of a chain of browser prompts.
      const addForm = (b, r, bucket, url) => {
        const hit = b.closest(".src-hit");
        hit.querySelector(".src-addform")?.remove();
        const tail = bucket ? bucket.uri.replace(/^(s3|gs):\/\//, "").split("/").filter(Boolean).pop() : (url.title || r.title);
        const f = document.createElement("div");
        f.className = "src-addform";
        f.innerHTML = `<label>Name <input class="af-name" value="${esc(slugName(tail, bucket ? "bucket" : "dataset"))}" autocomplete="off"></label>
          ${bucket ? `<label>Sub-folder <span class="dim">(optional)</span> <input class="af-sub" placeholder="e.g. csv/by_year" autocomplete="off"></label>
          <label>Only files matching <span class="dim">(optional)</span> <input class="af-inc" placeholder="*.csv, *.parquet" autocomplete="off"></label>` : ""}
          <button class="btn primary small af-go">${bucket ? "List and add" : "Test and add"}</button><button class="btn small af-x">Cancel</button><span class="dim af-msg" role="status"></span>`;
        b.closest("div").after(f);
        const nameIn = f.querySelector(".af-name"); nameIn.focus(); nameIn.select();
        f.querySelector(".af-x").onclick = () => f.remove();
        f.onkeydown = (e) => { if (e.key === "Enter") f.querySelector(".af-go").click(); if (e.key === "Escape") { e.stopPropagation(); f.remove(); b.focus(); } };
        const go = f.querySelector(".af-go");
        go.onclick = () => busy(go, async () => {
          const name = nameIn.value.trim();
          let body;
          if (bucket) {
            const sub = f.querySelector(".af-sub").value.trim().replace(/^\/+|\/+$/g, "");
            const include = f.querySelector(".af-inc").value.split(",").map((x) => x.trim()).filter(Boolean);
            body = { name, uri: sub ? bucket.uri.replace(/\/+$/, "") + "/" + sub : bucket.uri, kind: "public_bucket", options: { ...(include.length ? { include } : {}), ...(bucket.region ? { region: bucket.region } : {}) } };
          } else body = { name, uri: url.url, kind: "web" };
          Object.assign(body, { title: clip(r.title, 120), description: `${this.httpUrl(r.page) ? r.page : ""} (license: ${clip(r.license || "not stated", 100)})`.trim(), tags: [r.catalog] });
          f.querySelector(".af-msg").textContent = bucket ? "listing\u2026" : "testing\u2026";
          try {
            const s = await api("/api/sources", { method: "POST", body });
            toast(s.status === "ok" ? `Added ${s.name}${s.manifest ? ` (${s.manifest.file_count}${s.manifest.truncated ? "+" : ""} files)` : ""}` : `Saved ${s.name}, but: ${s.last_error}`, s.status === "ok" ? "ok" : "err");
            f.remove(); b.textContent = "added"; b.disabled = true;
            await this.load(); this.refreshTable(v);
          } catch (e) { f.querySelector(".af-msg").textContent = e.message; throw e; }
        });
      };
      $$("[data-addb]", box).forEach((b) => (b.onclick = () => { const [i, j] = b.dataset.addb.split(":").map(Number); addForm(b, out.results[i], out.results[i].buckets[j], null); }));
      $$("[data-add]", box).forEach((b) => (b.onclick = () => { const [i, j] = b.dataset.add.split(":").map(Number); addForm(b, out.results[i], null, out.results[i].files[j]); }));
    };
    $("#sd-go", d).onclick = () => busy($("#sd-go", d), run);
    $("#sd-q", d).onkeydown = (e) => { if (e.key === "Enter") $("#sd-go", d).click(); };
    $("#sd-q", d).focus();
  },

  form(v) {
    const f = $("#src-form", v); f.hidden = false;
    f.innerHTML = `<div class="src-card">
      <div class="src-grid">
        <label>Name<input id="sf-name" placeholder="noaa-ghcn" autocomplete="off"></label>
        <label>Location<input id="sf-uri" placeholder="https://..., gs://bucket/prefix, s3://bucket/prefix, gdrive://folder/ID, or ~/data/folder" autocomplete="off"></label>
        <label>Kind<select id="sf-kind"><option value="">auto</option>${Object.entries(this.kindLabel).map(([k, l]) => `<option value="${k}">${l}</option>`).join("")}</select></label>
        <label>Credentials<input id="sf-auth" placeholder="blank = public bucket (free, anonymous); Ceph: rclone:ceph" autocomplete="off"></label>
        <label>Lab staging<select id="sf-staging"><option value="auto">auto</option><option value="relay">relay (this machine uploads)</option><option value="direct">direct (cluster downloads)</option></select></label>
        <label>Level<select id="sf-level"><option value="">default</option><option>P1</option><option>P2</option><option>P3</option><option>P4</option></select></label>
        <label class="wide">Title<input id="sf-title" autocomplete="off"></label>
        <label class="wide">Only files matching (globs, comma separated)<input id="sf-include" placeholder="*.csv, *.parquet" autocomplete="off"></label>
      </div>
      <div class="src-actions"><button class="btn primary small" id="sf-save">Test and save</button><button class="btn small" id="sf-cancel">Cancel</button><span class="dim" id="sf-status" role="status"></span></div>
      <div class="dim">Local sources must be under ${esc(this.roots.join(", "))}. Ceph needs the campus VPN on this machine; Lab jobs get Ceph data by relay.</div></div>`;
    $("#sf-cancel", f).onclick = () => { f.hidden = true; };
    $("#sf-name", f).focus();
    const check = () => {
      const n = $("#sf-name", f).value.trim(), u = $("#sf-uri", f).value.trim();
      if (!/^[a-z0-9][a-z0-9-]{1,40}$/.test(n)) return "Name: 2-41 lowercase letters, digits or dashes";
      if (!u) return "Location is required";
      if (/^[a-z][a-z0-9+.-]*:/i.test(u) && !/^(https?|gs|s3|gdrive):\/\//i.test(u) && !/^(report|notebook):\d+$/i.test(u)) return `"${u.split(":")[0]}:" is not supported. Use https://, gs://, s3://, gdrive://, report:N, notebook:N, or a folder path`;
      return "";
    };
    f.onkeydown = (e) => { if (e.key === "Enter" && e.target.tagName === "INPUT") $("#sf-save", f).click(); };
    $("#sf-save", f).onclick = async () => {
      const bad = check(); if (bad) { $("#sf-status", f).textContent = bad; return; }
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
        <button class="btn small" id="sd-test">Test now</button><button class="btn small" id="sd-edit">Edit</button><button class="btn small danger" id="sd-del">Delete</button></div>
      <div id="sd-editform" hidden></div>
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
    $("#sd-test", v).onclick = () => busy($("#sd-test", v), async () => { try { const r = await api(`/api/sources/${id}/test`, { method: "POST" }); toast(r.status === "ok" ? "Reachable" : r.last_error, r.status === "ok" ? "ok" : "err"); } finally { this.renderOne(v, id); } });
    $("#sd-del", v).onclick = () => busy($("#sd-del", v), async () => {
      if (!(await confirmBox(`Delete source ${s.name}?`, "Removes it from your library (and its saved search index). The data itself is not touched, and reports that used it keep their record.", "Delete"))) return;
      await api(`/api/sources/${id}`, { method: "DELETE" });
      toast("Deleted", "ok");
      this.list = this.list.filter((x) => x.id !== s.id);
      closeTab(`src${id}`);
    });
    $("#sd-index", v).onclick = async () => {
      const b = $("#sd-index", v); b.disabled = true; b.textContent = "building\u2026";
      try { await api(`/api/sources/${id}/index`, { method: "POST" }); toast("Index saved; research runs will reuse it", "ok"); } catch (e) { toast(e.message, "err"); }
      this.renderOne(v, id);
    };
    const un = $("#sd-unindex", v);
    if (un) un.onclick = async () => { try { await api(`/api/sources/${id}/index`, { method: "DELETE" }); toast("Index deleted", "ok"); } catch (e) { toast(e.message, "err"); } this.renderOne(v, id); };
    // Edit what can change without re-adding the source (the name and location stay;
    // a new location is a new source). Filters or credentials re-test it.
    $("#sd-edit", v).onclick = () => {
      const f = $("#sd-editform", v);
      if (!f.hidden) { f.hidden = true; return; }
      const o = s.options || {};
      f.hidden = false;
      f.innerHTML = `<div class="src-card src-edit"><div class="src-grid">
        <label>Title<input id="se-title" value="${esc(s.title || "")}" autocomplete="off"></label>
        <label>Credentials<input id="se-auth" value="${esc(s.auth_ref || "")}" placeholder="blank = public" autocomplete="off"></label>
        <label>Lab staging<select id="se-staging">${["auto", "relay", "direct"].map((x) => `<option ${x === s.staging ? "selected" : ""}>${x}</option>`).join("")}</select></label>
        <label>Level<select id="se-level">${["P1", "P2", "P3", "P4"].map((x) => `<option ${x === s.protection_level ? "selected" : ""}>${x}</option>`).join("")}</select></label>
        <label class="wide">Description<input id="se-desc" value="${esc(s.description || "")}" autocomplete="off"></label>
        <label class="wide">Tags (comma separated)<input id="se-tags" value="${esc((s.tags || []).join(", "))}" autocomplete="off"></label>
        <label class="wide">Only files matching (globs)<input id="se-inc" value="${esc((o.include || []).join(", "))}" placeholder="*.csv, *.parquet" autocomplete="off"></label>
        <label class="wide">Skip files matching (globs)<input id="se-exc" value="${esc((o.exclude || []).join(", "))}" autocomplete="off"></label>
      </div><div class="src-actions"><button class="btn primary small" id="se-save">Save</button><button class="btn small" id="se-cancel">Cancel</button><span class="dim" id="se-status" role="status"></span></div></div>`;
      $("#se-title", f).focus();
      $("#se-cancel", f).onclick = () => { f.hidden = true; };
      const list = (id) => $(id, f).value.split(",").map((x) => x.trim()).filter(Boolean);
      const save = $("#se-save", f);
      save.onclick = () => busy(save, async () => {
        const inc = list("#se-inc"), exc = list("#se-exc");
        const opts = { ...o }; delete opts.include; delete opts.exclude;
        if (inc.length) opts.include = inc;
        if (exc.length) opts.exclude = exc;
        const retest = JSON.stringify(inc) !== JSON.stringify(o.include || []) || JSON.stringify(exc) !== JSON.stringify(o.exclude || []) || $("#se-auth", f).value.trim() !== (s.auth_ref || "");
        await api(`/api/sources/${id}`, { method: "PATCH", body: { title: $("#se-title", f).value.trim(), description: $("#se-desc", f).value.trim(), tags: list("#se-tags"), auth_ref: $("#se-auth", f).value.trim(), staging: $("#se-staging", f).value, protection_level: $("#se-level", f).value, options: opts } });
        if (retest) { $("#se-status", f).textContent = "re-listing\u2026"; await api(`/api/sources/${id}/test`, { method: "POST" }); }
        toast("Source updated", "ok");
        this.load().catch(() => {});
        this.renderOne(v, id);
      });
    };
    if (s.status === "unreachable") {
      // the error is already shown above; do not ask the source again for a listing
      $("#sd-items", v).innerHTML = `<div class="dim">Fix the problem above, then Test now.</div>`;
      return;
    }
    this.browse(v, id, "");
  },

  async browse(v, id, path) {
    const crumbs = ["", ...path.split("/").filter(Boolean)];
    $("#sd-crumbs", v).innerHTML = crumbs.map((c, i) => `<a data-p="${esc(crumbs.slice(1, i + 1).join("/"))}">${i ? esc(c) : "root"}</a>`).join(" / ");
    $$("#sd-crumbs a", v).forEach((a) => { a.href = "#"; a.onclick = (e) => { e.preventDefault(); this.browse(v, id, a.dataset.p); }; });
    const box = $("#sd-items", v); box.innerHTML = `<div class="dim">\u2026</div>`;
    let d;
    try { d = await api(`/api/sources/${id}/browse?path=${encodeURIComponent(path)}`); } catch (e) { box.innerHTML = `<div class="err-banner">${esc(e.message)}</div>`; return; }
    if (!d.items.length) { box.innerHTML = `<div class="dim">Empty.</div>`; return; }
    box.innerHTML = d.items.map((it) => `<div class="src-item ${it.dir ? "dir" : ""}" data-n="${esc(it.name)}" title="${esc(it.name)}"><span>${it.dir ? "\u{1F4C1} " : ""}${esc(it.name)}</span><span class="dim">${this.size(it.size)}</span></div>`).join("");
    $$(".src-item", box).forEach((el) => {
      const go = () => {
        const n = el.dataset.n, full = (path ? path + "/" : "") + n.replace(/\/$/, "");
        $$(".src-item.on", box).forEach((x) => x.classList.remove("on")); el.classList.add("on");
        if (el.classList.contains("dir")) this.browse(v, id, full); else this.preview(v, id, full);
      };
      el.tabIndex = 0; el.setAttribute("role", "button");
      el.onclick = go;
      el.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } };
    });
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
  picker(el, selected = [], onChange = null, opts = {}) {
    if (onChange && typeof onChange === "object") { opts = onChange; onChange = null; }
    const chosen = new Set(selected);
    const changed = () => { if (onChange) onChange([...chosen]); };
    const draw = () => {
      if (opts.compact && !this.list.length) { el.hidden = true; return; }
      el.innerHTML = `<div class="src-picker${opts.compact ? " compact" : ""}">${opts.compact ? '<span class="dim">Include data:</span>' : ""}${[...chosen].map((n) => `<span class="filechip">${esc(n)}<button data-rm="${esc(n)}">\u00d7</button></span>`).join("")}
        <select class="src-pick"><option value="">+ add a data source</option>${this.list.filter((s) => !chosen.has(s.name)).map((s) => `<option value="${esc(s.name)}">${esc(s.name)} (${esc(this.kindLabel[s.kind] || s.kind)}${s.status !== "ok" ? ", " + esc(s.status) : ""})</option>`).join("")}</select></div>`;
      $$("[data-rm]", el).forEach((b) => { b.setAttribute("aria-label", `Remove ${b.dataset.rm}`); b.onclick = () => { chosen.delete(b.dataset.rm); changed(); draw(); }; });
      const sel = $(".src-pick", el);
      sel.setAttribute("aria-label", "Add a data source");
      sel.onchange = (e) => { if (e.target.value) { chosen.add(e.target.value); changed(); draw(); } };
    };
    // Draw from what we have, then refresh: a source deleted or added elsewhere shows
    // up correctly, and a deleted one that was chosen is dropped.
    if (this.list.length) draw();
    this.load().then(() => {
      const known = new Set(this.list.map((x) => x.name));
      for (const n of [...chosen]) if (!known.has(n)) chosen.delete(n);
      draw();
    }).catch(() => { if (!this.list.length) el.innerHTML = `<span class="dim">Sources unavailable</span>`; });
    return () => [...chosen];
  },
};
