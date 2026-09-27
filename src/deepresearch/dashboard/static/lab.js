/* Lab runs: real computations on an HPC cluster, tied to the report.
   Loaded after features.js and before app.js; uses app.js helpers at call time. */
"use strict";

const LAB = {
  timers: {},
  logs: {},
  sid: null,

  // ------------------------------------------------------------------ panel
  async mount(el, s) {
    this.stopAll();
    this.sid = s.id;
    el.innerHTML = `<div class="lab-head"><h2>Lab runs</h2><span class="dim">Real computations on the cluster to test or extend this report</span></div><div class="lab-body"><div class="dim">\u2026</div></div>`;
    await this.refresh(el, s);
  },

  async refresh(el, s) {
    if (!el) return; // the panel is not on screen (report still running, other tab)
    let data;
    try { data = await api(`/api/sessions/${s.id}/lab`); }
    catch (e) { el.querySelector(".lab-body").innerHTML = `<div class="dim">${esc(e.message)}</div>`; return; }
    // A slow reply can land after the panel was re-rendered; wiring the detached copy
    // would cancel the live panel's poll timers and freeze its cards.
    if (LAB.sid !== s.id || !document.body.contains(el)) return;
    const body = el.querySelector(".lab-body");
    if (!data.configured) {
      body.innerHTML = `<div class="dim">No compute target configured. Add <span class="mono">lab_targets.json</span> to the settings folder.</div>`;
      return;
    }
    const sug = data.suggestions;
    body.innerHTML = `
      <div class="lab-actions">
        <button class="btn small primary" data-l="doc">\u2697 Lab run on this report</button>
        <span class="dim" style="font-size:11.5px">or select a passage and choose <b>Lab run</b></span>
      </div>
      <div class="lab-sug">${sug ? this.sugHtml(sug) : `<div class="lab-sug-empty"><button class="btn small" data-l="sug">Suggest computations for this report</button> <span class="dim" style="font-size:11px">Gemini reads the report and proposes up to 3 runnable jobs (about a cent)</span></div>`}</div>
      <div class="lab-runs">${data.runs.map((r) => this.runHtml(r)).join("")}</div>`;
    body.querySelector('[data-l="doc"]').onclick = () => this.startDialog(s, { scope: "document" });
    body.querySelector('[data-l="sug"]')?.addEventListener("click", (e) => this.loadSuggestions(el, s, e.target));
    body.querySelector('[data-l="resug"]')?.addEventListener("click", (e) => this.loadSuggestions(el, s, e.target, true));
    body.querySelectorAll("[data-sug]").forEach((b) => (b.onclick = () => {
      const x = sug.suggestions[+b.dataset.sug];
      this.startDialog(s, { scope: "suggestion", request: `${x.title}: ${x.question}\nApproach: ${x.approach}` });
    }));
    data.runs.forEach((r) => this.wire(el, s, r));
  },

  sugHtml(sug) {
    const items = sug.suggestions || [];
    return `<div class="label" style="margin-bottom:6px">Suggested computations <button class="linkbtn" data-l="resug">refresh</button></div>
      ${items.length ? items.map((x, i) => `
        <div class="lab-sug-card">
          <div class="t">${esc(x.title)}</div>
          <div class="q">${esc(x.question)}</div>
          <div class="a dim">${esc(x.approach)}</div>
          <div class="m mono dim">${esc((x.software || []).join(", "))}${x.est_runtime ? ` \u00b7 ~${esc(x.est_runtime)}` : ""}</div>
          ${x.why ? `<div class="w dim">${esc(x.why)}</div>` : ""}
          <button class="btn small" data-sug="${i}">Plan this run</button>
        </div>`).join("") : `<div class="dim">${esc(sug.note || "Nothing in this report looks computable.")}</div>`}
      ${items.length && sug.note ? `<div class="dim" style="font-size:11px;margin-top:4px">${esc(sug.note)}</div>` : ""}`;
  },

  async loadSuggestions(el, s, btn, refresh = false) {
    btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Reading the report';
    try { await api(`/api/sessions/${s.id}/lab/suggestions`, { method: "POST", body: { refresh } }); }
    catch (e) { toast(e.message, "err"); }
    this.refresh(el, s);
  },

  // ------------------------------------------------------------------ one run card
  runHtml(r) {
    const p = r.plan || {};
    const live = ["planning", "submitting", "queued", "running", "fetching", "analyzing"].includes(r.status);
    const badge = { draft: "review", plan_failed: "failed", planning: "planning", submitting: "running", queued: "queued", running: "running", fetching: "running", analyzing: "running" }[r.status] || r.status;
    const images = (r.files || []).filter((f) => !f.skipped && /\.(png|jpe?g|svg|gif)$/i.test(f.path));
    const others = (r.files || []).filter((f) => !images.includes(f));
    return `
    <div class="lab-run ${esc(r.status)}" data-run="${r.id}">
      <div class="lab-run-h">
        <span class="status-badge ${esc(badge)}">${esc(badge)}</span>
        <b>#${r.id} ${esc(p.title || (r.scope === "selection" ? "Selected passage" : "Whole report"))}</b>
        <span class="grow"></span>
        ${r.job_id ? `<span class="mono dim lab-meta">${this.metaText(r)}</span>` : ""}
      </div>
      ${live || r.status === "draft" ? `<div class="lab-stage">${live ? '<span class="spinner"></span>' : ""}<span>${esc(r.stage || r.status)}</span></div>` : ""}
      ${this.stepsHtml(r)}
      ${r.error && !["draft"].includes(r.status) ? `<div class="lab-err">${esc(r.error)}</div>` : ""}
      ${p.question ? `<div class="lab-q"><span class="label">Question</span> ${esc(p.question)}</div>` : ""}
      ${r.scope === "selection" && r.selection ? `<details class="lab-sel"><summary class="dim">Selected passage</summary><blockquote>${esc(clip(r.selection, 1200))}</blockquote></details>` : ""}
      ${r.status === "plan_failed" && p.why_not ? `<div class="lab-q dim">${esc(p.why_not)}</div>` : ""}
      ${r.result_md ? `<div class="lab-result md">${renderMd(this.plainMath(r.result_md))}</div>` : ""}
      ${images.length ? `<div class="lab-imgs">${images.map((f) => `<a href="${this.fileUrl(r.id, f.path)}" target="_blank" rel="noopener"><img src="${this.fileUrl(r.id, f.path)}" alt="${esc(f.path)}" loading="lazy"></a>`).join("")}</div>` : ""}
      ${others.length ? `<div class="lab-files">${others.map((f) => f.skipped
        ? `<span class="mono dim" title="left on the cluster (too large)">${esc(f.path)} (${this.size(f.size)}, not copied)</span>`
        : `<a class="mono" href="${this.fileUrl(r.id, f.path)}" target="_blank" rel="noopener">${esc(f.path)} <span class="dim">${this.size(f.size)}</span></a>`).join("")}</div>` : ""}
      <div class="lab-foot">
        ${r.status === "draft" ? `<button class="btn small primary" data-la="review">Review and submit</button>` : ""}
        ${r.status === "plan_failed" && !p.why_not ? `<button class="btn small primary" data-la="replan">\u21BB Retry plan</button>` : ""}
        ${p.script ? `<button class="btn small" data-la="plan">${r.status === "draft" ? "Plan" : "Plan and script"}</button>` : ""}
        ${r.job_id ? `<button class="btn small" data-la="log">${live ? "Live log" : "Log"}</button>` : ""}
        ${["completed", "failed", "cancelled"].includes(r.status) && p.script ? `<button class="btn small" data-la="rerun">\u21BB Re-run with changes</button>` : ""}
        ${r.result_md ? `<button class="btn small" data-la="nb">\u2192 Notebook</button>` : ""}
        <span class="grow"></span>
        <span class="mono dim" style="font-size:10.5px">${r.estimate_usd != null ? `compute est $${(+r.estimate_usd).toFixed(2)}` : ""}${r.ai_cost_usd ? ` \u00b7 AI $${(+r.ai_cost_usd).toFixed(2)}` : ""}${r.rerun_of ? ` \u00b7 re-run of #${r.rerun_of}` : ""}</span>
        ${live ? `<button class="btn small danger" data-la="cancel">Cancel</button>` : `<button class="btn small danger" data-la="del" title="Delete this lab run and its local results">Delete</button>`}
      </div>
      <div class="log lab-log" hidden></div>
    </div>`;
  },

  metaText(r) { return esc(`job ${r.job_id}${r.node ? " \u00b7 " + r.node : ""}${r.elapsed ? " \u00b7 " + r.elapsed : ""}`); },

  stepsHtml(r) {
    const order = ["planning", "draft", "queued", "running", "fetching", "analyzing", "completed"];
    const names = ["Plan", "Review", "Queued", "Running", "Fetch", "Write-up", "Done"];
    const at = { submitting: 2, plan_failed: 0 }[r.status] ?? order.indexOf(r.status);
    if (at < 0 && !["failed", "cancelled"].includes(r.status)) return "";
    const failAt = ["failed", "cancelled"].includes(r.status) ? (r.result_md ? 6 : r.job_id ? 3 : 1) : -1;
    return `<div class="lab-steps">${names.map((n, i) => {
      const cls = failAt >= 0 ? (i < failAt ? "done" : i === failAt ? "bad" : "") : (i < at ? "done" : i === at ? (r.status === "completed" ? "done" : "now") : "");
      return `<span class="${cls}">${n}</span>`;
    }).join("")}</div>`;
  },

  // Write-ups are asked for plain Unicode; turn stray inline TeX ($\theta$, $\le$) into symbols.
  plainMath(md) {
    const map = { theta: "\u03b8", alpha: "\u03b1", beta: "\u03b2", gamma: "\u03b3", delta: "\u03b4", Delta: "\u0394", lambda: "\u03bb", mu: "\u03bc", pi: "\u03c0", sigma: "\u03c3", phi: "\u03c6", psi: "\u03c8", omega: "\u03c9", le: "\u2264", leq: "\u2264", ge: "\u2265", geq: "\u2265", approx: "\u2248", times: "\u00d7", pm: "\u00b1", neq: "\u2260", infty: "\u221e", sqrt: "\u221a", cdot: "\u00b7", rightarrow: "\u2192", to: "\u2192" };
    return String(md || "").replace(/\$([^$\n]*\\[A-Za-z][^$\n]*)\$/g, (m, inner) => inner.replace(/\\([A-Za-z]+)/g, (t, w) => map[w] ?? t).replace(/[{}]/g, "").replace(/\^(\w)/g, "^$1"));
  },

  fileUrl(id, path) { return `/api/lab/${id}/file?path=${encodeURIComponent(path)}`; },
  size(n) { n = +n || 0; return n > 1e6 ? (n / 1e6).toFixed(1) + " MB" : n > 1e3 ? Math.round(n / 1e3) + " KB" : n + " B"; },

  wire(el, s, r) {
    const card = el.querySelector(`.lab-run[data-run="${r.id}"]`); if (!card) return;
    const act = (a) => card.querySelector(`[data-la="${a}"]`);
    act("review")?.addEventListener("click", () => this.review(el, s, r));
    act("plan")?.addEventListener("click", () => this.review(el, s, r, true));
    act("log")?.addEventListener("click", () => this.toggleLog(card, r));
    act("rerun")?.addEventListener("click", async () => {
      try { const n = await api(`/api/lab/${r.id}/rerun`, { method: "POST" }); await this.refresh(el, s); this.review(el, s, n); }
      catch (e) { toast(e.message, "err"); }
    });
    act("replan")?.addEventListener("click", async () => {
      try { await api(`/api/lab/${r.id}/replan`, { method: "POST" }); } catch (e) { toast(e.message, "err"); }
      this.refresh(el, s);
    });
    act("nb")?.addEventListener("click", () => NB.append(`## Lab run #${r.id}: ${r.plan?.title || ""}\n\n**Question:** ${r.plan?.question || ""}\n\n${r.result_md}\n\n*Source: Session #${s.id}, lab run #${r.id} (job ${r.job_id || "-"})*\n`));
    act("cancel")?.addEventListener("click", async () => {
      if (!(await confirmBox(`Cancel lab run #${r.id}?`, r.job_id ? `Slurm job ${r.job_id} will be cancelled on the cluster.` : "The plan will be discarded.", "Cancel run"))) return;
      try { await api(`/api/lab/${r.id}/cancel`, { method: "POST" }); } catch (e) { toast(e.message, "err"); }
      this.refresh(el, s);
    });
    act("del")?.addEventListener("click", async () => {
      if (!(await confirmBox(`Delete lab run #${r.id}?`, "Removes the run and its downloaded results from this computer. Files on the cluster are kept.", "Delete"))) return;
      try { await api(`/api/lab/${r.id}`, { method: "DELETE" }); } catch (e) { toast(e.message, "err"); }
      this.refresh(el, s);
    });
    const live = ["planning", "submitting", "queued", "running", "fetching", "analyzing"].includes(r.status);
    if (live) this.poll(el, s, r);
  },

  poll(el, s, r) {
    clearTimeout(this.timers[r.id]);
    this.timers[r.id] = setTimeout(async () => {
      if (LAB.sid !== s.id || !document.body.contains(el)) return;
      let n;
      try { n = await api(`/api/lab/${r.id}`); } catch { return this.poll(el, s, r); }
      const card = el.querySelector(`.lab-run[data-run="${r.id}"]`);
      if (!card) return;
      const logOpen = !card.querySelector(".lab-log").hidden;
      if (n.status === r.status && n.stage === r.stage && (n.elapsed !== r.elapsed || n.node !== r.node)) {
        // Only the clock or node moved: update that line, keep open log and details.
        const meta = card.querySelector(".lab-meta");
        if (meta) meta.innerHTML = this.metaText(n);
      } else if (n.status !== r.status || n.stage !== r.stage) {
        const tmp = document.createElement("div"); tmp.innerHTML = this.runHtml(n);
        card.replaceWith(tmp.firstElementChild);
        this.wire(el, s, n);
        const nc = el.querySelector(`.lab-run[data-run="${r.id}"]`);
        if (logOpen && n.job_id) this.toggleLog(nc, n);
        if (n.status === "completed" || n.status === "failed") {
          toast(`Lab run #${n.id} ${n.status}`, n.status === "completed" ? "ok" : "err");
          NOTIFY.send(`Lab run #${n.id} ${n.status}`, n.plan?.title || "");
        }
        if (n.status === "draft" && r.status === "planning") toast(`Lab run #${n.id}: plan ready for review`, "ok");
        return;
      }
      this.poll(el, s, n);
    }, r.status === "planning" ? 3000 : 5000);
  },

  async toggleLog(card, r) {
    const box = card.querySelector(".lab-log");
    if (!box.hidden) { box.hidden = true; clearTimeout(this.logs[r.id]); return; }
    box.hidden = false; box.textContent = "loading\u2026";
    let off = 0;
    const tick = async () => {
      if (box.hidden || !document.body.contains(box)) return;
      try {
        const d = await api(`/api/lab/${r.id}/log?offset=${off}`);
        if (off === 0) box.textContent = "";
        if (d.text) { const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 30; box.insertAdjacentHTML("beforeend", colorLog(d.text)); if (stick) box.scrollTop = box.scrollHeight; }
        if (!box.textContent) box.textContent = "(no output yet)";
        off = d.size || off;
      } catch (e) { box.insertAdjacentHTML("beforeend", `\n<span class="err">${esc(e.message)}</span>`); }
      const live = ["queued", "running", "fetching", "submitting"].includes(r.status);
      if (live) this.logs[r.id] = setTimeout(tick, 4000);
    };
    tick();
  },

  stopAll() {
    Object.values(this.timers).forEach(clearTimeout); this.timers = {};
    Object.values(this.logs).forEach(clearTimeout); this.logs = {};
  },

  // ------------------------------------------------------------------ start
  startDialog(s, opts) {
    const scope = opts.scope;
    const what = scope === "selection" ? `the highlighted passage (${fmtN(opts.selection.length)} characters)`
      : scope === "suggestion" ? "a suggested computation" : "the whole report";
    $("#modal").innerHTML = `
      <h3>\u2697 New lab run</h3>
      <div class="dim">Gemini reads ${what}, searches the web for the right software and method, and writes a job plan for <b>${esc(this.targetLabel || "the cluster")}</b>. Nothing runs until you review and submit it.</div>
      ${scope === "selection" ? `<blockquote class="lab-quote">${esc(clip(opts.selection, 700))}</blockquote>` : ""}
      <div class="field" style="margin-top:10px"><label>What should it compute? <span class="dim">(optional)</span></label>
        <textarea id="lab-req" rows="3" placeholder="e.g. verify the scaling claim with a real benchmark; keep it under an hour">${esc(opts.request || "")}</textarea></div>
      <div class="estimate"><span>PLANNING <b>~$0.10\u20130.30</b></span><span class="dim">Gemini Flash + Google Search ($14 per 1,000 searches), 1-2 min</span></div>
      <div class="acts"><button class="btn" data-x="0">Cancel</button><button class="btn primary" data-x="1">Write the plan</button></div>`;
    $("#modal-back").hidden = false;
    const close = () => ($("#modal-back").hidden = true);
    $('#modal [data-x="0"]').onclick = close;
    $("#modal-back").onclick = (e) => { if (e.target.id === "modal-back") close(); };
    $('#modal [data-x="1"]').onclick = async () => {
      const btn = $('#modal [data-x="1"]'); btn.disabled = true;
      try {
        await api(`/api/sessions/${s.id}/lab`, { method: "POST", body: { scope, selection: opts.selection || "", request: $("#lab-req").value } });
        close();
        toast("Planning started. Watch the Lab runs section at the end of the report.", "ok");
        const el = document.querySelector("#lab-panel");
        if (el) { await this.refresh(el, s); el.scrollIntoView({ behavior: "smooth", block: "start" }); }
      } catch (e) { toast(e.message, "err"); btn.disabled = false; }
    };
  },

  // ------------------------------------------------------------------ review
  review(el, s, r, readOnly = false) {
    const p = JSON.parse(JSON.stringify(r.plan || {}));
    const editable = r.status === "draft" && !readOnly;
    const res = p.resources || {};
    const params = p.parameters || {};
    const parts = this.partitions || {};
    const inst = p.install || {};
    $("#modal").innerHTML = `
      <div class="lab-review">
      <h3>${editable ? "Review lab run" : "Lab run"} #${r.id}: ${esc(p.title || "")}</h3>
      <div class="lab-q"><span class="label">Question</span> ${esc(p.question || "")}</div>
      <div class="lab-sec"><span class="label">Approach</span><div>${esc(p.approach || "")}</div></div>
      <div class="lab-grid">
        <div><span class="label">Software</span>${(p.software || []).map((x) => `<div><b>${esc(x.name)}</b> <span class="mono dim">${esc(x.source || "")}${x.version ? " " + esc(x.version) : ""}</span><div class="dim" style="font-size:11.5px">${esc(x.why || "")}</div></div>`).join("") || '<div class="dim">none</div>'}
          <div class="mono dim" style="font-size:10.5px;margin-top:4px">${[inst.modules?.length ? "modules: " + inst.modules.join(" ") : "", inst.conda?.length ? "conda: " + inst.conda.join(" ") : "", inst.pip?.length ? "pip: " + inst.pip.join(" ") : "", inst.apptainer?.length ? "containers: " + inst.apptainer.join(" ") : ""].filter(Boolean).join(" \u00b7 ")}</div></div>
        <div><span class="label">Inputs</span>${(p.inputs || []).map((x) => `<div style="font-size:12px">${esc(x)}</div>`).join("") || '<div class="dim">none</div>'}</div>
      </div>
      <div class="lab-sec"><span class="label">Parameters</span>
        <div class="lab-params">${Object.entries(params).map(([k, v]) => { const val = typeof v === "object" ? JSON.stringify(v) : String(v); return `<label ${val.length > 22 ? 'style="grid-column:span 2"' : ""}><span class="mono">${esc(k)}</span><input data-param="${esc(k)}" value="${esc(val)}" title="${esc(val)}" ${editable ? "" : "disabled"}></label>`; }).join("") || '<span class="dim">none</span>'}</div></div>
      <div class="lab-sec"><span class="label">Resources</span>
        <div class="lab-params res">
          <label><span class="mono">partition</span><select id="lr-part" ${editable ? "" : "disabled"}>${Object.entries(parts).map(([k, v]) => `<option value="${esc(k)}" ${k === res.partition ? "selected" : ""}>${esc(k)} \u00b7 ${esc(v.machine || "")} \u00b7 $${v.usd_per_hour}/h</option>`).join("") || `<option>${esc(res.partition || "")}</option>`}</select></label>
          <label><span class="mono">nodes</span><input id="lr-nodes" type="number" min="1" value="${esc(res.nodes || 1)}" ${editable ? "" : "disabled"}></label>
          <label><span class="mono">time limit</span><input id="lr-time" value="${esc(res.time_limit || "01:00:00")}" ${editable ? "" : "disabled"}></label>
          <label><span class="mono">gpus</span><input id="lr-gpus" type="number" min="0" value="${esc(res.gpus || 0)}" ${editable ? "" : "disabled"}></label>
        </div></div>
      <div class="lab-sec"><span class="label">Expected outputs</span> <span class="mono dim" style="font-size:11.5px">${esc((p.expected_outputs || []).join(", "))}</span></div>
      <div class="lab-sec"><span class="label">Success criteria</span><div class="dim" style="font-size:12px">${esc(p.success_criteria || "")}</div></div>
      ${p.caveats ? `<div class="lab-sec"><span class="label">Caveats</span><div class="dim" style="font-size:12px">${esc(p.caveats)}</div></div>` : ""}
      <details class="lab-sec" ${editable ? "" : "open"}><summary class="label">Run script (edit to change what runs)</summary>
        <textarea id="lr-script" class="lab-script" spellcheck="false" ${editable ? "" : "readonly"}>${esc(p.script || "")}</textarea></details>
      <details class="lab-sec"><summary class="label">Generated Slurm batch file</summary><pre class="lab-script">${esc(r.script || "")}</pre></details>
      <div class="estimate"><span>COMPUTE, WORST CASE <b id="lr-est">${r.estimate_usd != null ? "$" + (+r.estimate_usd).toFixed(2) : "?"}</b></span><span class="dim">nodes x time limit x list price; real jobs usually stop earlier</span>${r.ai_cost_usd ? `<span>AI so far <b>$${(+r.ai_cost_usd).toFixed(2)}</b></span>` : ""}</div>
      <div class="acts">
        <button class="btn" data-x="0">${editable ? "Close" : "Close"}</button>
        ${editable ? `<button class="btn" data-x="save">Save changes</button><button class="btn primary" data-x="submit">Submit to ${esc(r.target_label || "cluster")}</button>` : ""}
      </div>
      </div>`;
    $("#modal").classList.add("wide");
    $("#modal-back").hidden = false;
    const close = () => { $("#modal-back").hidden = true; $("#modal").classList.remove("wide"); };
    $('#modal [data-x="0"]').onclick = close;
    $("#modal-back").onclick = (e) => { if (e.target.id === "modal-back") close(); };
    if (!editable) return;
    const collect = () => {
      const np = JSON.parse(JSON.stringify(p));
      np.parameters = {};
      $$("#modal [data-param]").forEach((i) => {
        const orig = params[i.dataset.param];
        let v = i.value;
        if (typeof orig === "number" && v.trim() !== "" && !isNaN(+v)) v = +v;
        else if (typeof orig === "object") { try { v = JSON.parse(v); } catch { /* keep text */ } }
        np.parameters[i.dataset.param] = v;
      });
      np.resources = { ...(np.resources || {}), partition: $("#lr-part").value, nodes: Math.max(1, +$("#lr-nodes").value || 1), time_limit: $("#lr-time").value.trim(), gpus: Math.max(0, +$("#lr-gpus").value || 0) };
      np.script = $("#lr-script").value;
      return np;
    };
    const save = async () => { const n = await api(`/api/lab/${r.id}/plan`, { method: "PUT", body: { plan: collect() } }); $("#lr-est").textContent = n.estimate_usd != null ? "$" + (+n.estimate_usd).toFixed(2) : "?"; return n; };
    $('#modal [data-x="save"]').onclick = async () => { try { await save(); toast("Plan saved", "ok"); this.refresh(document.querySelector("#lab-panel"), s); } catch (e) { toast(e.message, "err"); } };
    $('#modal [data-x="submit"]').onclick = async () => {
      const b = $('#modal [data-x="submit"]'); b.disabled = true; b.innerHTML = '<span class="spinner"></span> Submitting';
      try {
        await save();
        const n = await api(`/api/lab/${r.id}/submit`, { method: "POST" });
        close(); toast(`Submitted: Slurm job ${n.job_id}`, "ok");
        NOTIFY.ask();
        this.refresh(document.querySelector("#lab-panel"), s);
      } catch (e) { toast(e.message, "err"); b.disabled = false; b.textContent = "Submit"; }
    };
  },

  async loadTargets() {
    try {
      const t = await api("/api/lab/targets");
      const first = t.targets[0];
      if (first) { this.targetLabel = first.label; this.partitions = first.partitions; }
    } catch { /* optional */ }
  },
};
