/* The Lab's Cluster view (v0.57.0): what the cluster is doing for us, through bifrost.
   Four sections, top to bottom: Now (live), Our jobs (14 days, Lab runs named),
   Spend and efficiency (30 days), Storage. Charts are plain inline SVG; no library.
   Loaded after lab.js and before app.js; uses app.js helpers at call time. */
"use strict";

const CLV = {
  timer: null,
  gen: 0,
  lab: null,

  stop() { clearTimeout(CLV.timer); CLV.gen++; },

  async render(v) {
    CLV.stop();
    const gen = CLV.gen;
    v.innerHTML = `<div class="clv">
      <div class="clv-head"><h2>Cluster</h2><span class="dim" id="clv-sub">Ursa Major through bifrost</span>
        <span class="grow"></span><button class="btn small" id="clv-refresh" title="Read again now">Refresh</button></div>
      <section class="clv-sec" id="clv-now"><div class="clv-sec-h"><h3>Now</h3><span class="clv-age" data-age="now"></span></div><div class="clv-body"><span class="spinner"></span></div></section>
      <section class="clv-sec" id="clv-jobs"><div class="clv-sec-h"><h3>Our jobs</h3><span class="dim">last 14 days</span><span class="clv-age" data-age="jobs"></span></div><div class="clv-body"><span class="spinner"></span></div></section>
      <section class="clv-sec" id="clv-spend"><div class="clv-sec-h"><h3>Spend and efficiency</h3><span class="dim">last 30 days</span><span class="clv-age" data-age="usage"></span></div><div class="clv-body"><span class="spinner"></span></div></section>
      <section class="clv-sec" id="clv-store"><div class="clv-sec-h"><h3>Storage</h3><span class="clv-age" data-age="storage"></span></div><div class="clv-body"><span class="spinner"></span></div></section>
    </div>`;
    v.querySelector("#clv-refresh").onclick = () => CLV.render(v);
    const live = () => gen === CLV.gen && v.isConnected;
    try { CLV.lab = await api("/api/cluster/lab?days=30"); } catch { CLV.lab = { jobs: {}, live: [] }; }
    const load = async (name) => {
      try { return await api(`/api/cluster/panel/${name}`); } catch (e) { return { error: e.message }; }
    };
    const fill = async (name, fn, again = 0) => {
      const r = await load(name);
      if (!live()) return;
      if (r.signed_in === false) return CLV.signedOut(v);
      CLV.age(v, name, r);
      if (r.loading) { setTimeout(() => live() && fill(name, fn, again + 1), Math.min(3000 + again * 2000, 15000)); return; }
      try { fn(r); } catch (e) { console.error(e); }
    };
    fill("now", (r) => CLV.now(v, r));
    Promise.all([load("jobs"), load("waste")]).then(([j, w]) => {
      if (!live()) return;
      if (j.signed_in === false) return CLV.signedOut(v);
      CLV.age(v, "jobs", j);
      if (j.loading) return fill("jobs", (r) => CLV.jobs(v, r, w));
      CLV.jobs(v, j, w);
    });
    fill("usage", (r) => CLV.spend(v, r));
    fill("storage", (r) => CLV.storage(v, r));
    // only "Now" changes minute to minute: re-read it every 30 s while the view is open
    const tick = () => { if (!live()) return; fill("now", (r) => CLV.now(v, r)); CLV.timer = setTimeout(tick, 30000); };
    CLV.timer = setTimeout(tick, 30000);
  },

  signedOut(v) {
    v.querySelector(".clv").innerHTML = `<div class="clv-head"><h2>Cluster</h2></div>
      <div class="clv-sec"><p>The Lab is not signed in to the cluster service (bifrost).</p>
      <p class="dim">Run <span class="mono">deep-research cluster login</span> in a terminal, then restart the dashboard.</p></div>`;
  },

  age(v, name, r) {
    const el = v.querySelector(`[data-age="${name}"]`); if (!el) return;
    if (r.loading) { el.innerHTML = '<span class="spinner"></span> reading the cluster\u2026'; return; }
    const a = r.age_s != null ? (r.age_s < 60 ? "just now" : `${Math.round(r.age_s / 60)} min ago`) : "";
    el.innerHTML = `${r.refreshing ? '<span class="spinner"></span> ' : ""}${a ? "as of " + a : ""}${r.error ? ` <span class="clv-err" title="${esc(r.error)}">read failed</span>` : ""}`;
  },

  money(x) { return x == null ? "\u2014" : "$" + (+x).toFixed(x >= 100 ? 0 : 2); },
  dur(s) { s = Math.round(s || 0); if (s < 60) return s + "s"; if (s < 3600) return Math.round(s / 60) + "m"; const h = Math.floor(s / 3600); return `${h}h ${Math.round((s % 3600) / 60)}m`; },

  // ---------------------------------------------------------------- Now
  now(v, r) {
    const d = r.data || {}, body = v.querySelector("#clv-now .clv-body");
    if (!r.data) { body.innerHTML = `<div class="dim">${esc(r.error || "No answer from the cluster.")}</div>`; return; }
    const parts = d.partitions || [];
    const lab = CLV.lab || { live: [] };
    const tiles = [
      ["Burning now", CLV.money(d.current_usd_per_hour) + "/h", d.current_usd_per_hour > 0.2 ? "warn" : ""],
      ["Nodes up", fmtN(d.nodes_powered_up), ""],
      ["Running", fmtN(d.jobs_running), d.jobs_running ? "go" : ""],
      ["Waiting", fmtN(d.jobs_pending), d.jobs_pending ? "warn" : ""],
      ["Our live Lab runs", fmtN(lab.live.length), lab.live.length ? "go" : ""],
    ];
    const problems = [...(d.problem_nodes || []), ...parts.flatMap((p) => (p.problems || []).map((x) => String(p.name) + ": " + String(x)))];
    body.innerHTML = `<div class="clv-tiles">${tiles.map(([k, val, c]) => `<div class="clv-tile ${c}"><div class="label">${k}</div><div class="clv-big">${esc(val)}</div></div>`).join("")}</div>
      <div class="clv-parts">${parts.map((p) => {
        const tot = Math.max(1, p.nodes_total || 0);
        const seg = (n, cls) => n ? `<i class="${cls}" style="width:${(n / tot) * 100}%" title="${n} ${cls}"></i>` : "";
        const idle = p.idle_powered_up || 0, alloc = p.allocated || 0, boot = p.booting || 0, down = p.down_or_drained || 0;
        return `<div class="clv-part"><span class="clv-pn mono">${esc(p.name)}</span>
          <span class="clv-bar" title="${p.nodes_total} nodes: ${alloc} busy, ${idle} idle and billing, ${boot} booting, ${p.powered_down || 0} off">${seg(alloc, "busy")}${seg(boot, "boot")}${seg(idle, "idle")}${seg(down, "down")}</span>
          <span class="clv-pm dim mono">${alloc + idle + boot}/${p.nodes_total || 0} up${p.jobs_running ? ` \u00b7 ${p.jobs_running} running` : ""}${p.jobs_pending ? ` \u00b7 ${p.jobs_pending} waiting` : ""}</span>
          <span class="clv-pc mono">${p.current_usd_per_hour ? CLV.money(p.current_usd_per_hour) + "/h" : `<span class="dim">${CLV.money(p.usd_per_node_hour)}/node-h</span>`}</span></div>`;
      }).join("")}</div>
      <div class="clv-legend dim"><i class="busy"></i>busy <i class="boot"></i>booting <i class="idle"></i>up, idle (billing) <i class="down"></i>down</div>
      ${lab.live.length ? `<div class="clv-sub-h">Our Lab runs in flight</div>${lab.live.map((x) => `<a class="clv-live" data-sid="${x.session_id}" data-rid="${x.run_id}"><span class="status-badge running">${esc(x.status)}</span> #${x.run_id} ${esc(clip(x.title || "", 70))} <span class="dim">${esc(clip(x.stage || "", 70))}</span></a>`).join("")}` : ""}
      ${problems.length ? `<div class="clv-sub-h">Needs attention</div><ul class="clv-prob">${problems.slice(0, 8).map((x) => `<li>${esc(clip(String(x), 200))}</li>`).join("")}</ul>` : ""}
      ${(d.idle_billing_nodes || []).length ? `<div class="dim clv-note">Billing with no job: ${esc(d.idle_billing_nodes.join(", "))}${d.idle_billing_nodes.every((x) => /checknodeset-0/.test(x)) ? " (the always-on check node, on purpose)" : ""}</div>` : ""}`;
    CLV.wireLive(body);
  },

  wireLive(el) {
    el.querySelectorAll("[data-rid]").forEach((a) => (a.onclick = () => { S.scrollToLab = +a.dataset.rid; openSession(+a.dataset.sid); }));
  },

  // ---------------------------------------------------------------- Our jobs
  jobs(v, r, w) {
    const body = v.querySelector("#clv-jobs .clv-body");
    const rows = Array.isArray(r.data) ? r.data : [];
    if (!r.data) { body.innerHTML = `<div class="dim">${esc(r.error || "No answer from the cluster.")}</div>`; return; }
    const labJobs = (CLV.lab || {}).jobs || {};
    // 14 day strip: jobs per day, stacked by how they ended
    const days = [];
    for (let i = 13; i >= 0; i--) { const d = new Date(Date.now() - i * 86400000); days.push(d.toISOString().slice(0, 10)); }
    const by = Object.fromEntries(days.map((d) => [d, { ok: 0, bad: 0, other: 0 }]));
    for (const j of rows) {
      const d = String(j.submitted || j.started || "").slice(0, 10); if (!by[d]) continue;
      const s = String(j.state || "");
      by[d][s === "COMPLETED" ? "ok" : /FAIL|TIMEOUT|OUT_OF_MEMORY|NODE_FAIL|BOOT_FAIL/.test(s) ? "bad" : "other"]++;
    }
    const top = Math.max(1, ...days.map((d) => by[d].ok + by[d].bad + by[d].other));
    const cols = days.map((d) => {
      const b = by[d], n = b.ok + b.bad + b.other;
      const seg = (k, lbl) => b[k] ? `<i class="b-${k}" style="height:${(b[k] / top) * 100}%" title="${d}: ${b[k]} ${lbl}"></i>` : "";
      const lbl = +d.slice(8) === 1 || d === days[0] ? new Date(d + "T12:00").toLocaleDateString(undefined, { month: "short", day: "numeric" }) : +d.slice(8);
      return `<div class="clv-col" title="${d}: ${n} jobs"><div class="clv-colbar">${seg("other", "cancelled or running")}${seg("bad", "failed")}${seg("ok", "completed")}</div><span>${lbl}</span></div>`;
    }).join("");
    const total = rows.length, ok = rows.filter((j) => j.state === "COMPLETED").length;
    const failed = rows.filter((j) => /FAIL|TIMEOUT|OUT_OF_MEMORY/.test(String(j.state || ""))).length;
    const ofLab = rows.filter((j) => labJobs[j.job_id]).length;
    const running = rows.filter((j) => ["RUNNING", "PENDING", "CONFIGURING", "COMPLETING"].includes(j.state));
    const wd = (w && w.data) || {};
    const wkinds = Object.entries(wd.count_by_kind || {}).sort((a, b) => b[1] - a[1]);
    const recent = rows.slice(0, 12);
    const kind = (j) => { const l = labJobs[j.job_id]; if (!l) return /^(bifrost-env-check|probe-|urls-)/.test(j.name || "") ? "Lab check" : ""; return l.why.startsWith("pilot") ? "pilot" : l.why === "full" ? "Lab run" : l.why; };
    body.innerHTML = `<div class="clv-tiles">
        <div class="clv-tile"><div class="label">Jobs</div><div class="clv-big">${fmtN(total)}</div><div class="dim">${fmtN(ofLab)} from the Lab</div></div>
        <div class="clv-tile go"><div class="label">Completed</div><div class="clv-big">${total ? Math.round((ok / total) * 100) : 0}%</div><div class="dim">${fmtN(ok)} jobs</div></div>
        <div class="clv-tile ${failed ? "bad" : ""}"><div class="label">Failed</div><div class="clv-big">${fmtN(failed)}</div><div class="dim">incl. timeouts, out of memory</div></div>
        <div class="clv-tile ${running.length ? "go" : ""}"><div class="label">Running or waiting</div><div class="clv-big">${fmtN(running.length)}</div></div>
      </div>
      <div class="clv-chart" role="img" aria-label="Jobs per day, last 14 days"><div class="clv-cols"><span class="clv-ymax mono dim">${top}</span><span class="clv-y0 mono dim">0</span><i class="clv-grid"></i>${cols}</div>
        <div class="clv-legend dim"><i class="ok"></i>completed <i class="bad"></i>failed <i class="other"></i>cancelled or running</div></div>
      <div class="clv-sub-h">Latest</div>
      <table class="clv-table"><thead><tr><th>Job</th><th>What</th><th>Partition</th><th>State</th><th class="clv-num">Took</th><th>When</th></tr></thead><tbody>
      ${recent.map((j) => { const l = labJobs[j.job_id]; const k = kind(j);
        return `<tr ${l ? `data-sid="${l.session_id}" data-rid="${l.run_id}" class="clv-link"` : ""}><td class="mono">${esc(j.job_id)}</td>
        <td>${k ? `<span class="clv-kind">${esc(k)}</span> ` : ""}${esc(clip(l ? `#${l.run_id} ${l.title}` : j.name || "", 60))}</td>
        <td class="mono dim">${esc(j.partition || "")}</td>
        <td><span class="clv-st ${esc(String(j.state || "").toLowerCase())}">${esc(String(j.state || "").toLowerCase())}</span>${j.exit_code && j.exit_code !== "0" && j.exit_code !== "0:0" ? ` <span class="dim mono">exit ${esc(j.exit_code)}</span>` : ""}</td>
        <td class="mono dim clv-num">${j.elapsed_s ? CLV.dur(j.elapsed_s) : "\u2014"}</td><td class="dim">${esc(ago(j.submitted))}</td></tr>`; }).join("")}
      </tbody></table>
      ${wkinds.length ? `<div class="clv-sub-h">Avoidable spend, last 7 days <span class="dim">(${CLV.money(wd.total_est_wasted_usd)}, ${fmtN(wd.total_wasted_node_hours)} node-hours)</span></div>
        <div class="clv-waste">${wkinds.map(([k, n]) => `<span class="clv-chip" title="${esc(CLV.wasteText[k] || "")}">${esc(k.replace(/-/g, " "))} <b>${n}</b> job${n === 1 ? "" : "s"}</span>`).join("")}</div>
        <div class="dim clv-note">${esc((wd.items || []).slice(0, 1).map((x) => `Biggest: job ${x.job_id}, ${x.detail} (${CLV.money(x.est_wasted_usd)}).`).join(""))}</div>` : ""}`;
    CLV.wireLive(body);
  },
  wasteText: {
    "warm-worker": "Keep-warm jobs holding a node (the old warm Lab node). Retired when bifrost runs the Lab.",
    "low-cpu": "Jobs that used little of the CPU they held.",
    "failed-fast-repeat": "The same job failing quickly several times in a row.",
    "idle-node": "Nodes powered up with no job.",
    "timeout-idle": "Jobs that hit their time limit while mostly idle.",
  },

  // ---------------------------------------------------------------- Spend
  spend(v, r) {
    const body = v.querySelector("#clv-spend .clv-body");
    if (!r.data) { body.innerHTML = `<div class="dim">${esc(r.error || "No answer from the cluster.")}</div>`; return; }
    const rows = (r.data.rows || []).slice().sort((a, b) => (b.est_cost_usd || 0) - (a.est_cost_usd || 0));
    const total = rows.reduce((s, x) => s + (x.est_cost_usd || 0), 0);
    const jobs = rows.reduce((s, x) => s + (x.jobs || 0), 0);
    const ch = rows.reduce((s, x) => s + (x.core_hours || 0), 0);
    const eff = ch ? rows.reduce((s, x) => s + (x.cpu_efficiency_percent || 0) * (x.core_hours || 0), 0) / ch : 0;
    const top = Math.max(1, ...rows.map((x) => x.est_cost_usd || 0));
    const lab = CLV.lab || {};
    const oc = lab.outcome || {};
    const ocs = [["confirmed", "go"], ["refuted", "warn"], ["inconclusive", "neu"], ["broken", "bad"]];
    const ocTotal = ocs.reduce((s, [k]) => s + (oc[k] || 0), 0);
    body.innerHTML = `<div class="clv-tiles">
        <div class="clv-tile"><div class="label">Cluster spend</div><div class="clv-big">${CLV.money(total)}</div><div class="dim">${fmtN(jobs)} jobs, ${fmtN(Math.round(ch))} core-hours</div></div>
        <div class="clv-tile ${eff < 30 ? "warn" : "go"}"><div class="label">CPU efficiency</div><div class="clv-big">${Math.round(eff)}%</div><div class="dim">of the cores we held</div></div>
        <div class="clv-tile"><div class="label">Lab runs</div><div class="clv-big">${fmtN(lab.runs || 0)}</div><div class="dim">${fmtN(ocTotal)} finished with an outcome; ${CLV.money(lab.ai_usd)} AI, \u2264 ${CLV.money(lab.estimate_usd)} cluster worst case</div></div>
      </div>
      <div class="clv-two">
        <div><div class="clv-sub-h">By partition</div>${rows.map((x) => `<div class="clv-hbar" title="${esc(x.key)}: ${fmtN(x.jobs)} jobs, ${fmtN(x.failed)} failed, ${fmtN(x.core_hours)} core-hours, CPU ${x.cpu_efficiency_percent}%">
          <span class="mono">${esc(x.key)}</span><span class="clv-track"><i style="width:${x.est_cost_usd ? Math.max(1.5, ((x.est_cost_usd || 0) / top) * 100) : 0}%"></i></span>
          <span class="mono">${CLV.money(x.est_cost_usd)}</span><span class="dim mono clv-eff ${x.cpu_efficiency_percent < 30 ? "low" : ""}">${Math.round(x.cpu_efficiency_percent || 0)}% cpu</span></div>`).join("") || '<div class="dim">No jobs.</div>'}</div>
        <div><div class="clv-sub-h">Lab outcomes</div>${ocTotal ? `<div class="clv-stack">${ocs.map(([k, c]) => oc[k] ? `<i class="${c}" style="width:${(oc[k] / ocTotal) * 100}%" title="${k}: ${oc[k]}"></i>` : "").join("")}</div>
          <div class="clv-legend dim">${ocs.map(([k, c]) => `<span><i class="${c}"></i>${k} ${oc[k] || 0}</span>`).join(" ")}</div>` : '<div class="dim">No finished Lab runs in 30 days.</div>'}
          <div class="dim clv-note">Spend is bifrost's estimate (list price \u00d7 the share of each node held); it leaves out boot and idle time and credits.</div></div>
      </div>`;
  },

  // ---------------------------------------------------------------- Storage
  storage(v, r) {
    const body = v.querySelector("#clv-store .clv-body");
    if (!r.data) { body.innerHTML = `<div class="dim">${esc(r.error || "No answer from the cluster.")}</div>`; return; }
    const d = r.data, gb = (b) => (b / 1e9).toFixed(b >= 1e10 ? 0 : 1) + " GB";
    const fs = d.filesystems || [];
    const big = (d.largest || []).slice(0, 8);
    const topB = Math.max(1, ...big.map((x) => x.bytes || 0));
    body.innerHTML = `<div class="clv-two">
      <div><div class="clv-sub-h">Shared filesystems</div>${fs.map((f) => `<div class="clv-hbar"><span class="mono">${esc(f.mount)}</span><span class="clv-track"><i class="${f.used_pct > 85 ? "bad" : f.used_pct > 70 ? "warn" : ""}" style="width:${Math.max(1, f.used_pct)}%"></i></span><span class="mono">${f.used_pct}%</span><span class="dim mono">${gb(f.free_bytes)} free</span></div>`).join("")}
        <div class="dim clv-note">Our home folder: ${d.home_bytes != null ? gb(d.home_bytes) : "\u2014"}${d.scratch_bytes != null ? `, scratch ${gb(d.scratch_bytes)}` : ""}.</div></div>
      <div><div class="clv-sub-h">Largest in our home</div>${big.map((x) => `<div class="clv-hbar"><span class="mono clv-path" title="${esc(x.path)}">${esc(String(x.path).replace(/^\/home\/[^/]+\//, "~/"))}</span><span class="clv-track"><i style="width:${(x.bytes / topB) * 100}%"></i></span><span class="mono">${gb(x.bytes)}</span></div>`).join("") || '<div class="dim">\u2014</div>'}</div>
    </div>`;
  },

  // ---------------------------------------------------------------- report Live log: our cluster jobs
  async jobsBox(s) {
    // the Live log tab of a report: the report's Lab runs and their Slurm jobs, live
    const el = $("#lablive"); if (!el) return 0;
    let data;
    try { data = await api(`/api/sessions/${s.id}/lab`); } catch { return 0; }
    if (!el.isConnected) return 0;
    const runs = (data.runs || []).filter((r) => r.status !== "draft" || r.job_id);
    const active = ["planning", "submitting", "smoke", "queued", "running", "fetching", "analyzing"];
    const live = runs.filter((r) => active.includes(r.status));
    const recent = runs.filter((r) => !active.includes(r.status)).slice(0, 3);
    if (!live.length && !recent.length) { el.innerHTML = ""; return 0; }
    const row = (r) => {
      const p = r.plan || {};
      const jobs = (r.cluster_jobs || []).slice(-3).map((j) => `<span class="mono dim">job ${esc(j.job_id)}${j.partition ? " on " + esc(j.partition) : ""}${j.why && j.why !== "full" ? " (" + esc(j.why) + ")" : ""}</span>`).join(" \u00b7 ");
      const badge = { draft: "review", plan_failed: "failed", submitting: "running", smoke: "pilot", fetching: "running", analyzing: "running" }[r.status] || r.status;
      return `<a class="ll-run" data-rid="${r.id}"><div><span class="status-badge ${esc(badge)}">${esc(badge)}</span>${LAB.outcomeBadge(r)} <b>#${r.id}</b> ${esc(clip(p.title || "", 90))}</div>
        <div class="dim">${esc(clip(r.error || r.stage || "", 120))}${r.node ? ` <span class="mono">\u00b7 ${esc(r.node)}</span>` : ""}${r.elapsed ? ` <span class="mono">\u00b7 ${esc(r.elapsed)}</span>` : ""}</div>${jobs ? `<div>${jobs}</div>` : ""}</a>`;
    };
    el.innerHTML = `<div class="label">Cluster jobs for this report</div>${live.map(row).join("")}${recent.length ? `<details class="ll-more"${live.length ? "" : " open"}><summary class="dim">Recent finished</summary>${recent.map(row).join("")}</details>` : ""}
      <a class="dim ll-clv" id="ll-clv">Open the Cluster view \u2192</a>`;
    el.querySelectorAll("[data-rid]").forEach((a) => (a.onclick = () => { S.scrollToLab = +a.dataset.rid; renderStage(); }));
    el.querySelector("#ll-clv").onclick = () => openCluster();
    // a run on the cluster right now goes to the top of the tab; finished ones sit below
    // the report's own timeline
    const parent = el.parentElement, tl = $("#timeline");
    if (parent && tl) {
      if (live.length && parent.firstElementChild !== el) parent.insertBefore(el, parent.firstElementChild);
      else if (!live.length && tl.nextElementSibling !== el) tl.after(el);
      el.classList.toggle("top", !!live.length);
    }
    return live.length;
  },
};
