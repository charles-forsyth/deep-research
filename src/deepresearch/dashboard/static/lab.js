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
        <span class="lab-cat dim" style="font-size:11px;margin-left:auto"></span>
      </div>
      <div class="lab-sug">${sug ? this.sugHtml(sug) : `<div class="lab-sug-empty"><button class="btn small" data-l="sug">Suggest computations for this report</button> <span class="dim" style="font-size:11px">Gemini reads the report and proposes up to 3 runnable jobs (about a cent)</span></div>`}</div>
      <div class="lab-runs">${data.runs.map((r) => this.runHtml(r)).join("")}</div>`;
    if (S.scrollToLab) {  // arrived from the Lab runs page: show that run
      const card = body.querySelector(`.lab-run[data-run="${S.scrollToLab}"]`);
      S.scrollToLab = null;
      // top of the card (title and status) under the toolbar, not the middle of a tall card
      if (card) setTimeout(() => { card.style.scrollMarginTop = "12px"; card.scrollIntoView({ behavior: "smooth", block: "start" }); card.classList.add("flash"); card.tabIndex = -1; card.focus({ preventScroll: true }); }, 150);
    }
    body.querySelector('[data-l="doc"]').onclick = () => this.startDialog(s, { scope: "document" });
    this.catalogLine(body.querySelector(".lab-cat"));
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

  // Cluster catalog: what the AI is told about the cluster (modules, recipes, GPU)
  catalogText(c) {
    if (!c || !c.available) return c && c.reason ? "" : "cluster info: not loaded";
    const gpu = c.gpu && c.gpu.driver ? ` \u00b7 GPU driver ${c.gpu.driver} (CUDA ${c.gpu.cuda_max})` : "";
    const when = (c.generated || "").slice(0, 16).replace("T", " ");
    return `cluster info: ${c.modules} modules, ${c.recipes} recipes${gpu} \u00b7 ${when} UTC`;
  },
  async catalogLine(span) {
    if (!span) return;
    let c;
    try { c = await api("/api/lab/catalog"); } catch { return; }
    if (!c.available && c.reason) return; // target has no catalog configured
    span.innerHTML = `${esc(this.catalogText(c))} <button class="linkbtn" data-l="catref">refresh</button>`;
    span.querySelector('[data-l="catref"]').onclick = async (e) => {
      e.target.disabled = true; e.target.textContent = "refreshing\u2026";
      try {
        const n = await api("/api/lab/catalog/refresh", { method: "POST", body: {} });
        toast("Cluster info refreshed", "ok");
        this.partitions = null; // re-read partitions (prices, GPUs) on next review
        span.innerHTML = `${esc(this.catalogText(n))} <button class="linkbtn" data-l="catref">refresh</button>`;
        this.catalogLine(span);
      } catch (err) { toast(err.message, "err"); e.target.disabled = false; e.target.textContent = "refresh"; }
    };
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
    const live = ["planning", "submitting", "smoke", "queued", "running", "fetching", "analyzing"].includes(r.status);
    const badge = { draft: "review", plan_failed: "failed", planning: "planning", submitting: "running", smoke: "pilot", queued: "queued", running: "running", fetching: "running", analyzing: "running" }[r.status] || r.status;
    // SVG can carry scripts, so it is listed as a download, never shown inline
    const images = (r.files || []).filter((f) => !f.skipped && /\.(png|jpe?g|gif|webp)$/i.test(f.path));
    const others = (r.files || []).filter((f) => !images.includes(f));
    return `
    <div class="lab-run ${esc(r.status)}" data-run="${r.id}">
      <div class="lab-run-h">
        <span class="status-badge ${esc(badge)}">${esc(badge)}</span>
        ${this.outcomeBadge(r)}
        <b>#${r.id} ${esc(p.title || (r.scope === "selection" ? "Selected passage" : "Whole report"))}</b>
        <span class="grow"></span>
        ${r.job_id ? `<span class="mono dim lab-meta">${this.metaText(r)}</span>` : ""}
        ${r.provenance ? `<span class="mono dim lab-fp" title="Fingerprint of the script, software, resources and data (with content hashes) this run used${(r.provenance.sources || []).length ? ": " + esc(r.provenance.sources.map((d) => d.name + "@" + (d.manifest_hash || "").slice(0, 8)).join(", ")) : ""}">inputs ${esc(r.provenance.fingerprint)}</span>` : ""}
      </div>
      ${live || r.status === "draft" ? `<div class="lab-stage">${live ? '<span class="spinner"></span>' : ""}<span>${esc(r.stage || r.status)}</span></div>` : ""}
      ${this.stepsHtml(r)}
      ${r.error && (r.status !== "draft" || r.stage === "Not submitted") ? `<div class="lab-err">${r.status === "draft" ? "Not submitted: " : ""}${esc(r.error)}</div>` : ""}
      ${p.question ? `<div class="lab-q"><span class="label">Question</span> ${esc(p.question)}</div>` : ""}
      ${r.scope === "selection" && r.selection ? `<details class="lab-sel"><summary class="dim">Selected passage</summary><blockquote>${esc(clip(r.selection, 1200))}</blockquote></details>` : ""}
      ${r.status === "plan_failed" && p.why_not ? `<div class="lab-q dim">${esc(p.why_not)}</div>` : ""}
      ${this.smokeHtml(r)}
      ${this.verdictHtml(r.verdict, r.assessment)}
      ${r.result_md ? `<div class="lab-result md">${renderMd(this.plainMath(r.result_md))}</div>` : ""}
      ${images.length ? `<div class="lab-imgs">${images.map((f) => `<a href="${this.fileUrl(r.id, f.path)}" target="_blank" rel="noopener"><img src="${this.fileUrl(r.id, f.path)}" alt="${esc(f.path)}" loading="lazy"></a>`).join("")}</div>` : ""}
      ${others.length ? `<div class="lab-files">${others.map((f) => f.skipped
        ? `<span class="mono dim" title="left on the cluster (too large)">${esc(f.path)} (${this.size(f.size)}, not copied)</span>`
        : `<a class="mono" href="${this.fileUrl(r.id, f.path)}${/\.(svg|html?)$/i.test(f.path) ? "&download=1" : ""}" target="_blank" rel="noopener noreferrer">${esc(f.path)} <span class="dim">${this.size(f.size)}</span></a>`).join("")}</div>` : ""}
      <div class="lab-foot">
        ${r.status === "draft" ? `<button class="btn small primary" data-la="review">Review and submit</button>` : ""}
        ${r.status === "plan_failed" && !p.why_not ? `<button class="btn small primary" data-la="replan">\u21BB Retry plan</button>` : ""}
        ${p.script ? `<button class="btn small" data-la="plan">${r.status === "draft" ? "Plan" : "Plan and script"}</button>` : ""}
        ${r.job_id ? `<button class="btn small" data-la="log">${live ? "Live log" : "Log"}</button>` : ""}
        ${r.status === "failed" && p.script ? `<button class="btn small primary" data-la="fixfailed" title="The AI reads the error in the job log and fixes only what failed. You review the new plan before anything runs.">Fix with AI</button>` : ""}
        ${["completed", "failed", "cancelled"].includes(r.status) && p.script ? `<button class="btn small" data-la="rerun">\u21BB Re-run with changes</button>` : ""}
        ${r.result_md ? `<button class="btn small" data-la="nb">\u2192 Notebook</button>` : ""}
        <span class="grow"></span>
        <span class="mono dim" style="font-size:10.5px">${r.estimate_usd != null ? `compute est $${(+r.estimate_usd).toFixed(2)}` : ""}${r.ai_cost_usd ? ` \u00b7 AI $${(+r.ai_cost_usd).toFixed(2)}` : ""}${r.rerun_of ? ` \u00b7 re-run of #${r.rerun_of}` : ""}</span>
        ${live ? `<button class="btn small danger" data-la="cancel">Stop run</button>` : `<button class="btn small danger" data-la="del" title="Delete this lab run and its local results">Delete</button>`}
      </div>
      <div class="log lab-log" hidden></div>
    </div>`;
  },

  metaText(r) { return esc(`job ${r.job_id}${r.node ? " \u00b7 " + r.node : ""}${r.elapsed ? " \u00b7 " + r.elapsed : ""}`); },

  stepsHtml(r) {
    const order = ["planning", "draft", "queued", "running", "fetching", "analyzing", "completed"];
    const names = ["Plan", "Review", "Queued", "Running", "Fetch", "Write-up", "Done"];
    const at = { submitting: 2, smoke: 2, plan_failed: 0 }[r.status] ?? order.indexOf(r.status);
    if (at < 0 && !["failed", "cancelled"].includes(r.status)) return "";
    // Where it stopped: before submit (1), in the job (3 = Running: the job itself
    // failed or was cancelled, even when a write-up of the failure exists), fetching
    // (4) or writing up (5). "Done" is never the red step.
    const stage = (r.stage || "").toLowerCase();
    const failAt = !["failed", "cancelled"].includes(r.status) ? -1
      : !r.job_id ? 1
      : /fetch/.test(stage) ? 4
      : /write-up|writing up|analy/.test(stage) && /(^|\b)(could not|failed)/.test(stage) ? 5
      : 3;
    return `<div class="lab-steps">${names.map((n, i) => {
      const cls = failAt >= 0 ? (i < failAt ? "done" : i === failAt ? "bad" : "") : (i < at ? "done" : i === at ? (r.status === "completed" ? "done" : "now") : "");
      return `<span class="${cls}">${n}</span>`;
    }).join("")}</div>`;
  },

  // Write-ups are asked for plain Unicode; turn stray inline TeX ($\theta$, $\le$) into symbols.
  concernsHtml(p) {
    // what an AI fix did that needs a person's eyes (fallback to a reference value, changed
    // verdict tolerances, a swapped formula variable, a removed library, a big rewrite)
    const cs = p.fix_concerns || [];
    if (!cs.length) return "";
    return `<div class="lab-warn" role="alert" style="margin:6px 0"><b>Check before running:</b><ul>${cs.map((c) => `<li>${esc(c)}</li>`).join("")}</ul></div>`;
  },
  smokeHtml(r) {
    // smoke test rounds on the warm node, and the plan changes the AI made to pass them
    const sm = r.smoke;
    const p = r.plan || {};
    const bits = [];
    if (sm && Array.isArray(sm.rounds) && sm.rounds.length) {
      bits.push(`<span class="label">Pilot</span> ` + sm.rounds.map((x) => `round ${x.round}: ${x.passed ? "passed" : `<b>failed</b>${x.class && x.class !== "script" ? ` (${esc(({install: "software setup", container: "container image", "tool-crash": "program crashed", "missing-feature": "missing feature", glibc: "binary too new for the nodes", numerical: "numerical blow-up", timeout: "time limit", oom: "out of memory"})[x.class] || x.class)})` : ""}`}${x.rc != null ? ` (exit ${x.rc}${x.seconds != null ? `, ${x.seconds}s` : ""})` : ""}${(x.missing || []).length ? `, missing ${esc(x.missing.join(", "))}` : ""}${x.note ? ` <span class="dim">${esc(x.note)}</span>` : ""}`).join("; "));
    }
    if (sm && sm.pilot) bits.push(`<span class="label">Pilot result</span> <b>${esc(String(sm.pilot.outcome || "").toUpperCase())}</b>: ${esc(sm.pilot.why || "")} The full run was not started.`);
    if ((p.fix_concerns || []).length && r.status === "draft") bits.push(`<span class="label">Check before running</span> ${p.fix_concerns.map(esc).join("; ")}`);
    if (p.partition_switched) bits.push(`<span class="label">Partition</span> ${esc(p.partition_switched)}`);
    if (String(r.job_id || "").startsWith("warm:")) bits.push(`<span class="label">Ran on</span> the warm Lab node (no node boot)`);
    if (!bits.length) return "";
    return `<div class="lab-q dim" style="font-size:11.5px">${bits.join("<br>")}</div>`;
  },
  outcomeBadge(r) {
    const a = r.assessment;
    if (!a || !a.outcome) return "";
    const tip = { confirmed: "The claim held, with validation and design checks passing", refuted: "A real negative result: the checks were sound and the claim did not hold", inconclusive: "The test could not tell (e.g. both arms saturated); not evidence either way", broken: "A validation check or the job failed, so the numbers are not findings" }[a.outcome] || "";
    return `<span class="outcome-badge ${esc(a.outcome)}" title="${esc(tip)}">${esc(a.outcome)}</span>`;
  },
  verdictHtml(v, a) {
    // outputs/verdict.json: the job's own checks, grouped by kind, with the outcome
    if (!v || !Array.isArray(v.checks)) return "";
    const row = (c) => `<li>${c.pass === true ? "pass" : c.pass === false ? "<b>FAIL</b>" : "?"}: ${esc(c.name || "")}${c.expected !== undefined ? ` (expected ${esc(JSON.stringify(c.expected))}, got ${esc(JSON.stringify(c.got))})` : ""}</li>`;
    const labels = { validation: "Validation (the model is sane)", informative: "Informative (the test can tell)", claim: "Claim (the report's claim itself)" };
    const groups = a && a.groups ? Object.entries(labels).filter(([k]) => (a.groups[k] || []).length)
      .map(([k, t]) => `<div class="vk"><span class="dim" style="font-size:11px">${t}</span><ul>${a.groups[k].slice(0, 12).map(row).join("")}</ul></div>`).join("")
      : `<ul>${v.checks.slice(0, 12).map(row).join("")}</ul>`;
    // the dashboard re-checks the job's own verdict: contradictions, loose tolerances,
    // comparison arms that produced identical numbers
    const audit = Array.isArray(v.audit) && v.audit.length
      ? `<span class="label">Our re-check of this verdict:</span><ul>${v.audit.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`
      : "";
    const o = a && a.outcome;
    const cls = { confirmed: audit ? "lab-warn" : "lab-fixed", refuted: "lab-refuted", inconclusive: "lab-warn", broken: "lab-err" }[o] || (v.pass === false ? "lab-err" : "lab-fixed");
    const head = o ? `<span class="label">Outcome: ${esc(o.toUpperCase())}</span><div style="font-size:12px;margin:2px 0 4px">${esc(a.why || "")}${a.inferred ? ' <span class="dim">(check kinds inferred from their names)</span>' : ""}</div>` : `<span class="label">Checks: no overall verdict</span>`;
    return `<div class="${cls} lab-verdict">${head}${groups}${audit}</div>`;
  },
  plainMath(md) {
    const map = { theta: "\u03b8", alpha: "\u03b1", beta: "\u03b2", gamma: "\u03b3", delta: "\u03b4", Delta: "\u0394", lambda: "\u03bb", mu: "\u03bc", pi: "\u03c0", sigma: "\u03c3", phi: "\u03c6", psi: "\u03c8", omega: "\u03c9", le: "\u2264", leq: "\u2264", ge: "\u2265", geq: "\u2265", approx: "\u2248", times: "\u00d7", pm: "\u00b1", neq: "\u2260", infty: "\u221e", sqrt: "\u221a", cdot: "\u00b7", rightarrow: "\u2192", to: "\u2192" };
    return String(md || "").replace(/\$([^$\n]*\\[A-Za-z][^$\n]*)\$/g, (m, inner) => inner.replace(/\\([A-Za-z]+)/g, (t, w) => map[w] ?? t).replace(/[{}]/g, "").replace(/\^(\w)/g, "^$1"));
  },

  fileUrl(id, path) { return WS.q(`/api/lab/${id}/file?path=${encodeURIComponent(path)}`); },
  size(n) { n = +n || 0; return n > 1e6 ? (n / 1e6).toFixed(1) + " MB" : n > 1e3 ? Math.round(n / 1e3) + " KB" : n + " B"; },

  wire(el, s, r) {
    const card = el.querySelector(`.lab-run[data-run="${r.id}"]`); if (!card) return;
    const act = (a) => card.querySelector(`[data-la="${a}"]`);
    act("review")?.addEventListener("click", () => this.review(el, s, r));
    act("plan")?.addEventListener("click", () => this.review(el, s, r, true));
    act("log")?.addEventListener("click", () => this.toggleLog(card, r));
    // Every action runs once at a time (busy() disables the button until it ends).
    const on = (a, fn) => { const b = act(a); if (b) b.addEventListener("click", () => busy(b, fn)); };
    on("rerun", async () => { const n = await api(`/api/lab/${r.id}/rerun`, { method: "POST" }); await this.refresh(el, s); this.review(el, s, n); });
    on("fixfailed", async () => {
      const b = act("fixfailed"); b.innerHTML = '<span class="spinner"></span> Reading the log';
      const n = await api(`/api/lab/${r.id}/fix-failed`, { method: "POST" });
      const f = n.fix || {};
      toast(`New draft #${n.id}: ${(f.changes || []).length} change(s)${(f.remaining || []).length ? `, ${f.remaining.length} warning(s)` : ""}`, (f.remaining || []).length ? "err" : "ok");
      await this.refresh(el, s); this.review(el, s, n);
    });
    on("replan", async () => { try { await api(`/api/lab/${r.id}/replan`, { method: "POST" }); } finally { this.refresh(el, s); } });
    on("nb", () => NB.append(`## Lab run #${r.id}: ${r.plan?.title || ""}\n\n**Question:** ${r.plan?.question || ""}\n\n${r.result_md}\n\n*Source: Session #${s.id}, lab run #${r.id} (job ${r.job_id || "-"})*\n`));
    on("cancel", async () => {
      if (!(await confirmBox(`Stop lab run #${r.id}?`, r.job_id ? `Slurm job ${r.job_id} will be cancelled on the cluster.` : "The plan will be discarded.", "Stop run", "Keep running"))) return;
      try { await api(`/api/lab/${r.id}/cancel`, { method: "POST" }); } finally { this.refresh(el, s); }
    });
    on("del", async () => {
      if (!(await confirmBox(`Delete lab run #${r.id}?`, "Removes the run and its downloaded results from this computer. Files on the cluster are kept.", "Delete"))) return;
      try { await api(`/api/lab/${r.id}`, { method: "DELETE" }); } finally { this.refresh(el, s); }
    });
    const live = ["planning", "submitting", "smoke", "queued", "running", "fetching", "analyzing"].includes(r.status);
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
      const live = ["queued", "running", "fetching", "submitting", "smoke"].includes(r.status);
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
    MODAL.open(`
      <h3>\u2697 New lab run</h3>
      <div class="dim">Gemini reads ${what}, searches the web for the right software and method, and writes a job plan for <b>${esc(this.targetLabel || "the cluster")}</b>. Nothing runs until you review and submit it.</div>
      ${scope === "selection" ? `<blockquote class="lab-quote">${esc(clip(opts.selection, 700))}</blockquote>` : ""}
      <div class="field" style="margin-top:10px"><label>What should it compute? <span class="dim">(optional)</span></label>
        <textarea id="lab-req" rows="3" placeholder="e.g. verify the scaling claim with a real benchmark; keep it under an hour">${esc(opts.request || "")}</textarea></div>
      <div class="field"><label>Data to include <span class="dim">(optional; staged read-only on the cluster, the plan reads it from $DS_NAME)</span></label>
        <div id="lab-ds"></div></div>
      <div class="estimate"><span>PLANNING <b>~$0.10\u20130.30</b></span><span class="dim">Gemini Flash + Google Search ($14 per 1,000 searches), 1-2 min</span></div>
      <div class="acts"><button class="btn" data-x="0">Cancel</button><button class="btn primary" data-x="1">Write the plan</button></div>`,
      { dirty: () => ($("#lab-req")?.value || "").trim() !== (opts.request || "").trim() });
    const pd = s.project_defaults;
    const pickedSources = typeof SRC !== "undefined" ? SRC.picker($("#lab-ds"), opts.data_sources || (pd ? pd.data_sources : [])) : () => [];
    if (pd) $("#lab-ds").insertAdjacentHTML("afterend", `<div class="dim" style="font-size:11px;margin-top:4px">Defaults from project \u201c${esc(pd.title)}\u201d${pd.lab_partition ? `; partition ${esc(pd.lab_partition)} preferred` : ""}.</div>`);
    const close = () => MODAL.close();
    $('#modal [data-x="0"]').onclick = () => MODAL.requestClose();
    $("#lab-req").focus();
    const go = $('#modal [data-x="1"]');
    go.onclick = () => busy(go, async () => {
      await api(`/api/sessions/${s.id}/lab`, { method: "POST", body: { scope, selection: opts.selection || "", request: $("#lab-req").value, data_sources: pickedSources() } });
      close();
      toast("Planning started. Watch the Lab runs section at the end of the report.", "ok");
      const el = document.querySelector("#lab-panel");
      if (el) { await this.refresh(el, s); el.scrollIntoView({ behavior: "smooth", block: "start" }); }
    });
  },

  // ------------------------------------------------------------------ review
  fieldDiff(f) {
    // objects: one line per changed key ("partition: spot -> standard"); else raw values
    let a, b;
    try { a = JSON.parse(f.before); b = JSON.parse(f.after); } catch { a = b = undefined; }
    const show = (v) => (v === undefined ? "(none)" : typeof v === "string" ? v : JSON.stringify(v));
    if (a && b && typeof a === "object" && typeof b === "object" && !Array.isArray(a) && !Array.isArray(b)) {
      const keys = [...new Set([...Object.keys(a), ...Object.keys(b)])].filter((k) => JSON.stringify(a[k]) !== JSON.stringify(b[k]));
      return keys.map((k) => `<div class="chg"><span class="dim">${esc(k)}:</span> <span class="del">${esc(show(a[k]))}</span> \u2192 <span class="add">${esc(show(b[k]))}</span></div>`).join("");
    }
    return `<div class="del">${esc(f.before)}</div><div class="add">${esc(f.after)}</div>`;
  },

  diffHtml(d) {
    if (!d || (!d.script && !(d.fields || []).length)) return "";
    const lines = (d.script || "").split("\n").filter((l) => !/^(---|\+\+\+) (before|after)$/.test(l));
    const body = lines.map((l) => `<span class="${l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : l.startsWith("@@") ? "hunk" : ""}">${esc(l)}</span>`).join("\n");
    const n = lines.filter((l) => /^[+-]/.test(l)).length;
    return `<details class="lab-diff"><summary class="dim">What changed: ${n} script line${n === 1 ? "" : "s"}${(d.fields || []).length ? `, ${d.fields.length} other field${d.fields.length === 1 ? "" : "s"}` : ""}</summary>
      ${(d.fields || []).map((f) => `<div class="lab-diff-field"><span class="mono">${esc(f.key)}</span>${this.fieldDiff(f)}</div>`).join("")}
      ${d.script ? `<pre class="lab-script diff">${body}</pre>` : ""}${d.truncated ? '<div class="dim" style="font-size:11px">diff shortened</div>' : ""}</details>`;
  },

  review(el, s, r, readOnly = false) {
    const p = JSON.parse(JSON.stringify(r.plan || {}));
    const editable = r.status === "draft" && !readOnly;
    const res = p.resources || {};
    const params = p.parameters || {};
    const parts = this.partitions || {};
    const inst = p.install || {};
    let snapshot = null; // form state at open; closing with changes asks first
    MODAL.open(`
      <div class="lab-review">
      <div class="modal-head"><h3>${editable ? "Review lab run" : "Lab run"} #${r.id}: ${esc(p.title || "")}</h3><button class="icon-btn modal-x" data-x="0" aria-label="Close">\u2715</button></div>
      <nav class="lab-toc">${["What and why", "Software and data", "Settings", "Result check", "Script"].map((t, i) => `<a href="#" data-sec="${i + 1}">${i + 1}. ${t}</a>`).join("")}</nav>
      <div class="lab-q"><span class="label">Question</span> ${esc(p.question || "")}</div>
      ${(p.warnings || []).length ? `<div class="lab-warn"><span class="label">Checked against the cluster: ${p.warnings.length} problem${p.warnings.length > 1 ? "s" : ""}</span><ul>${p.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul><div class="lab-fix-row">${editable ? `<button class="btn" data-x="fix" title="The AI fixes only what is flagged, then the plan is checked again. Nothing is submitted.">Fix with AI</button>` : ""}<span class="dim" style="font-size:11px">${editable ? "or edit the plan (modules, partition, GPUs) yourself, or submit anyway." : ""}</span></div></div>` : ""}
      ${!p.plan_before_fix && (p.fix_changes || []).length && editable ? `<div class="lab-fixed"><div class="lab-fix-row"><span class="label">AI fix of failed run #${esc(r.rerun_of || "")}</span> <span class="dim" style="font-size:11.5px">${(p.warnings || []).length ? (p.warnings.length + " warning" + (p.warnings.length > 1 ? "s" : "") + " left") : "checks pass"}</span></div><ul>${p.fix_changes.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>${this.concernsHtml(p)}${p.fix_notes ? `<div class="dim" style="font-size:11.5px">${esc(p.fix_notes.replace(/REVIEW: [^.]*\. ?/g, ""))}</div>` : ""}${this.diffHtml(p.fix_diff)}</div>` : ""}
      ${p.plan_before_fix && editable ? `<div class="lab-fixed"><div class="lab-fix-row"><span class="label">Fixed by AI and re-checked</span> <span class="dim" style="font-size:11.5px">${(p.warnings || []).length ? (p.warnings.length + " warning" + (p.warnings.length > 1 ? "s" : "") + " left") : "no warnings"}</span> <button class="btn" data-x="undofix">Undo fix</button></div>${(p.fix_changes || []).length ? `<ul>${p.fix_changes.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}${this.concernsHtml(p)}${p.fix_notes ? `<div class="dim" style="font-size:11.5px">${esc(p.fix_notes.replace(/REVIEW: [^.]*\. ?/g, ""))}</div>` : ""}${this.diffHtml(p.fix_diff)}</div>` : ""}
      <h4 class="lab-h" id="lr-sec-1">1. What and why</h4>
      <div class="lab-sec"><span class="label">Approach</span><div>${esc(p.approach || "")}</div></div>
      <h4 class="lab-h" id="lr-sec-2">2. Software and data</h4>
      <div class="lab-grid">
        <div><span class="label">Software</span>${(p.software || []).map((x) => `<div><b>${esc(x.name)}</b> <span class="mono dim">${esc(x.source || "")}${x.version ? " " + esc(x.version) : ""}</span><div class="dim" style="font-size:11.5px">${esc(x.why || "")}</div></div>`).join("") || '<div class="dim">none</div>'}
          <div class="mono dim" style="font-size:10.5px;margin-top:4px">${[inst.modules?.length ? "modules: " + inst.modules.join(" ") : "", inst.conda?.length ? "conda: " + inst.conda.join(" ") : "", inst.pip?.length ? "pip: " + inst.pip.join(" ") : "", inst.apptainer?.length ? "containers: " + inst.apptainer.join(" ") : ""].filter(Boolean).join(" \u00b7 ")}</div></div>
        <div><span class="label">Data sources <span class="dim" style="text-transform:none;letter-spacing:0">staged read-only, read via $DS_NAME</span></span><div id="lr-ds">${(p.data_sources || []).map((n) => `<span class="filechip">${esc(n)}</span>`).join(" ") || '<div class="dim">none</div>'}</div>
          <span class="label" style="margin-top:8px;display:block">Inputs</span>${(p.inputs || []).map((x) => `<div style="font-size:12px">${esc(x)}</div>`).join("") || '<div class="dim">none</div>'}</div>
      </div>
      <h4 class="lab-h" id="lr-sec-3">3. Settings</h4>
      <div class="lab-sec"><span class="label">Parameters</span>
        <div class="lab-params">${Object.entries(params).map(([k, v]) => { const val = typeof v === "object" ? JSON.stringify(v) : String(v); return `<label ${val.length > 22 ? 'style="grid-column:span 2"' : ""}><span class="mono">${esc(k)}</span><input data-param="${esc(k)}" value="${esc(val)}" title="${esc(val)}" ${editable ? "" : "disabled"}></label>`; }).join("") || '<span class="dim">none</span>'}</div></div>
      ${Object.keys(p.parameter_sources || {}).length ? `<div class="lab-sec"><span class="label">Where the parameters come from</span><ul class="lab-psrc">${Object.entries(p.parameter_sources).map(([k, v]) => `<li><span class="mono">${esc(k)}</span>: <span class="${/^assumed/i.test(String(v)) ? "bad" : "dim"}">${esc(String(v))}</span></li>`).join("")}</ul></div>` : (editable ? `<div class="lab-sec dim" style="font-size:11.5px">No parameter sources given: values may be unjustified.</div>` : "")}
      <div class="lab-sec"><span class="label">Resources</span>
        <div class="lab-params res">
          <label><span class="mono">partition</span><select id="lr-part" ${editable ? "" : "disabled"}>${Object.entries(parts).map(([k, v]) => `<option value="${esc(k)}" ${k === res.partition ? "selected" : ""}>${esc(k)} \u00b7 ${esc(v.cpus ? v.cpus + " cores" : (v.machine || ""))}${v.gpus ? " + " + v.gpus + " GPU" : ""}${v.spot ? " \u00b7 spot" : ""} \u00b7 $${v.usd_per_hour}/h</option>`).join("") || `<option>${esc(res.partition || "")}</option>`}</select></label>
          <label><span class="mono">nodes</span><input id="lr-nodes" type="number" min="1" value="${esc(res.nodes || 1)}" ${editable ? "" : "disabled"}></label>
          <label><span class="mono">time limit</span><input id="lr-time" value="${esc(res.time_limit || "01:00:00")}" ${editable ? "" : "disabled"}></label>
          <label><span class="mono">gpus</span><input id="lr-gpus" type="number" min="0" value="${esc(res.gpus || 0)}" ${editable ? "" : "disabled"}></label>
        </div></div>
      <h4 class="lab-h" id="lr-sec-4">4. Result check</h4>
      <div class="lab-sec"><span class="label">Expected outputs</span> <span class="mono dim" style="font-size:11.5px">${esc((p.expected_outputs || []).join(", "))}</span></div>
      <div class="lab-sec"><span class="label">Success criteria</span><div class="dim" style="font-size:12px">${esc(p.success_criteria || "")}</div></div>
      ${p.caveats ? `<div class="lab-sec"><span class="label">Caveats</span><div class="dim" style="font-size:12px">${esc(p.caveats)}</div></div>` : ""}
      <h4 class="lab-h" id="lr-sec-5">5. Script</h4>
      <details class="lab-sec" open><summary class="label">Run script ${editable ? "(edit to change what runs)" : ""} <span class="dim">\u2014 click to fold</span></summary>
        <textarea id="lr-script" class="lab-script" spellcheck="false" ${editable ? "" : "readonly"}>${esc(p.script || "")}</textarea></details>
      <details class="lab-sec"><summary class="label">Generated Slurm batch file <span class="dim">\u2014 click to show</span></summary><pre class="lab-script">${esc(r.script || "")}</pre></details>
      <div class="estimate"><span>COMPUTE, WORST CASE <b id="lr-est">${r.estimate_usd != null ? "$" + (+r.estimate_usd).toFixed(2) : "?"}</b></span><span class="dim">nodes x time limit x list price; real jobs usually stop earlier</span>${r.ai_cost_usd ? `<span>AI so far <b>$${(+r.ai_cost_usd).toFixed(2)}</b></span>` : ""}</div>
      <div class="acts">
        <button class="btn" data-x="0">Close</button>
        ${editable ? `<button class="btn" data-x="save">Save draft</button><span class="grow"></span><button class="btn primary" data-x="submit">Submit to ${esc(r.target_label || "cluster")}\u2026</button>` : ""}
      </div>
      </div>`, { cls: "wide", dirty: () => editable && snapshot !== null && snapshot !== formState() });
    $$("#modal .lab-toc a").forEach((a) => (a.onclick = (e) => { e.preventDefault(); $("#lr-sec-" + a.dataset.sec)?.scrollIntoView({ behavior: "smooth", block: "start" }); }));
    const close = () => MODAL.close();
    $$('#modal [data-x="0"]').forEach((b) => (b.onclick = () => MODAL.requestClose()));
    const formState = () => JSON.stringify([...document.querySelectorAll("#modal input, #modal select, #modal textarea")].map((i) => i.value));
    if (!editable) return;
    const pickedDs = typeof SRC !== "undefined" ? SRC.picker($("#lr-ds"), p.data_sources || []) : () => p.data_sources || [];
    const collect = () => {
      const np = JSON.parse(JSON.stringify(p));
      const ds = pickedDs();
      if (ds.length) np.data_sources = ds; else delete np.data_sources;
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
      delete np.warnings; // recomputed by the server on save
      return np;
    };
    const save = async () => {
      const n = await api(`/api/lab/${r.id}/plan`, { method: "PUT", body: { plan: collect() } });
      snapshot = formState();
      $("#lr-est").textContent = n.estimate_usd != null ? "$" + (+n.estimate_usd).toFixed(2) : "?";
      const w = (n.plan && n.plan.warnings) || [];
      if (w.length) toast(`Saved. ${w.length} cluster check warning${w.length > 1 ? "s" : ""}: ${w[0]}`, "err");
      return n;
    };
    snapshot = formState();
    const fixBtn = $('#modal [data-x="fix"]');
    if (fixBtn) fixBtn.onclick = () => busy(fixBtn, async () => {
      fixBtn.innerHTML = '<span class="spinner"></span> Fixing';
      const n = await api(`/api/lab/${r.id}/fix`, { method: "POST" });
      const f = n.fix || {};
      toast((f.remaining || []).length ? `Plan fixed; ${f.remaining.length} warning(s) left` : (f.rounds ? "Plan fixed; checks pass" : (f.notes || "No problems found")), (f.remaining || []).length ? "err" : "ok");
      snapshot = formState(); // the old form is replaced; nothing to discard
      this.review(el, s, n);
      this.refresh(document.querySelector("#lab-panel"), s);
    });
    const undoBtn = $('#modal [data-x="undofix"]');
    if (undoBtn) undoBtn.onclick = () => busy(undoBtn, async () => {
      const n = await api(`/api/lab/${r.id}/undo-fix`, { method: "POST" });
      toast("Restored the plan from before the AI fix", "ok");
      snapshot = formState();
      this.review(el, s, n);
      this.refresh(document.querySelector("#lab-panel"), s);
    });
    const saveBtn = $('#modal [data-x="save"]');
    saveBtn.onclick = () => busy(saveBtn, async () => { await save(); toast("Draft saved", "ok"); this.refresh(document.querySelector("#lab-panel"), s); });
    const subBtn = $('#modal [data-x="submit"]');
    subBtn.onclick = () => busy(subBtn, async () => {
      // A paid action: say what it costs and where it goes, and make it a second click.
      const est = $("#lr-est").textContent;
      if (!subBtn.dataset.armed) {
        subBtn.dataset.armed = "1"; subBtn.dataset.keepLabel = "1";
        subBtn.innerHTML = `Confirm: submit (worst case ${esc(est)})`;
        subBtn.classList.add("armed");
        setTimeout(() => { if (subBtn.isConnected && subBtn.dataset.armed) { delete subBtn.dataset.armed; delete subBtn.dataset.keepLabel; subBtn.classList.remove("armed"); subBtn.innerHTML = `Submit to ${esc(r.target_label || "cluster")}\u2026`; } }, 6000);
        return;
      }
      delete subBtn.dataset.armed; subBtn.classList.remove("armed");
      subBtn.innerHTML = '<span class="spinner"></span> Submitting';
      try {
        await save();
        const n = await api(`/api/lab/${r.id}/submit`, { method: "POST" });
        snapshot = formState(); close(); toast(`Submitted: Slurm job ${n.job_id}`, "ok");
        NOTIFY.ask();
        this.refresh(document.querySelector("#lab-panel"), s);
      } catch (e) {
        delete subBtn.dataset.keepLabel;
        subBtn.innerHTML = `Submit to ${esc(r.target_label || "cluster")}\u2026`;
        throw e;
      }
    });
  },

  async loadTargets() {
    try {
      const t = await api("/api/lab/targets");
      this.targetsList = t.targets;
      const first = t.targets[0];
      if (first) { this.targetLabel = first.label; this.partitions = first.partitions; }
    } catch { /* optional */ }
  },

  async warmBox() {
    // the warm Lab node: one long-lived job that runs smoke tests, checks and short runs
    const box = $("#warm-box");
    if (!box) return;
    try {
      const w = await api("/api/lab/warm");
      if (!w.enabled) { box.textContent = ""; return; }
      const ws = (w.workers || []).map((x) => `job ${esc(x.job)} ${esc(x.state.toLowerCase())}${x.node ? " on " + esc(x.node) : ""}${x.left ? `, ${esc(x.left)} left` : ""}${x.busy.length ? `, busy: ${esc(x.busy.join(", "))}` : ", idle"}${x.draining ? " (draining)" : ""}`).join("; ");
      box.innerHTML = `<b>Warm Lab node</b> (${esc(w.partition || "")}): ${ws || "not running (starts on the next submit)"}; ${w.queued} queued, ${w.running} running. It stops after 20 idle minutes. <button class="btn small" id="warm-start">Start now</button> ${ws ? '<button class="btn small danger" id="warm-stop">Stop</button>' : ""}`;
      const st = $("#warm-start"), sp = $("#warm-stop");
      if (st) st.onclick = () => busy(st, async () => { await api("/api/lab/warm/start", { method: "POST" }); toast("Warm Lab node requested", "ok"); this.warmBox(); });
      if (sp) sp.onclick = () => busy(sp, async () => { await api("/api/lab/warm/stop", { method: "POST" }); toast("Warm Lab node stopping", "ok"); setTimeout(() => this.warmBox(), 4000); });
    } catch (e) { box.textContent = "Warm Lab node: " + e.message; }
  },

  // ------------------------------------------------------------------ all runs page
  async renderAll(v) {
    v.innerHTML = `<div class="runs-view"><div class="runs-head"><h2>Lab runs</h2>
      <select id="runs-filter" aria-label="Filter lab runs"><option value="">All</option><option value="live">Running or queued</option><option value="draft">Waiting for review</option><option value="completed">Completed</option><option value="failed">Failed</option><option value="cancelled">Cancelled</option></select>
      <span class="grow"></span><span class="dim" id="runs-count"></span></div>
      <div id="warm-box" class="dim" style="font-size:12px;margin:6px 0 10px"></div>
      <div id="runs-body"><span class="spinner"></span></div></div>`;
    this.warmBox();
    const live = ["planning", "submitting", "smoke", "queued", "running", "fetching", "analyzing"];
    const draw = (runs) => {
      const f = $("#runs-filter").value;
      const shown = runs.filter((r) => !f || (f === "live" ? live.includes(r.status) : f === "failed" ? ["failed", "plan_failed"].includes(r.status) : r.status === f));
      $("#runs-count").textContent = `${shown.length} of ${runs.length}`;
      $("#runs-body").innerHTML = shown.length ? `<table class="runs-table"><thead><tr><th>#</th><th>Status</th><th>What</th><th>Report</th><th>Where</th><th title="worst case: nodes x time limit x list price">Max cost</th><th>Updated</th></tr></thead><tbody>${shown.map((r) => {
        const p = r.plan || {};
        const badge = { draft: "review", plan_failed: "failed", submitting: "running", smoke: "pilot", fetching: "running", analyzing: "running" }[r.status] || r.status;
        const cost = r.estimate_usd != null ? "\u2264 $" + (+r.estimate_usd).toFixed(2) : "";
        return `<tr data-sid="${r.session_id}" data-rid="${r.id}">
          <td class="mono">${r.id}</td>
          <td><span class="status-badge ${esc(badge)}">${esc(badge)}</span>${LAB.outcomeBadge(r)}</td>
          <td><div class="runs-title">${esc(p.title || (r.scope === "selection" ? "Selected passage" : "Whole report"))}</div>${(() => {
            // the badge already says the status; only show a stage line that adds something
            const line = r.error ? clip(r.error, 140) : (r.stage || "");
            const redundant = !r.error && (/^(ended|done|completed|cancelled|failed)\b/i.test(line) || line.toLowerCase() === badge);
            return line && !redundant ? `<div class="dim runs-stage">${esc(line)}</div>` : "";
          })()}</td>
          <td class="runs-report" title="${esc(r.session_title || "")}">#${r.session_id} ${esc(clip(oneLine(r.session_title || ""), 80))}</td>
          <td class="mono dim">${esc((p.resources || {}).partition || "")}${r.job_id ? `<div>job ${esc(r.job_id)}</div>` : ""}</td>
          <td class="mono dim">${esc(cost)}</td>
          <td class="dim">${esc(ago(r.updated_at))}</td></tr>`;
      }).join("")}</tbody></table>` : `<div class="dim" style="padding:20px">No lab runs${f ? " match this filter" : " yet. Start one from any report"}.</div>`;
      $$("#runs-body tr[data-sid]").forEach((tr) => {
        const open = () => { S.scrollToLab = +tr.dataset.rid; openSession(+tr.dataset.sid); };
        tr.tabIndex = 0; tr.setAttribute("role", "link");
        tr.onclick = open;
        tr.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
      });
    };
    try {
      const { runs } = await api("/api/lab/runs");
      $("#runs-filter").onchange = () => draw(runs);
      draw(runs);
    } catch (e) { $("#runs-body").innerHTML = `<div class="err-banner">${esc(e.message)}</div>`; }
  },
};
