/* Deep Research // v0.17 features: citation cards, read-aloud, audio export,
   briefs, actual cost, live timeline, research map, compare, notifications.
   Loaded after app.js; uses its helpers ($, api, esc, toast, renderMd ...). */
"use strict";

// ---------------------------------------------------------------- citations
// Reports end with "**Sources:**\n1. [label](url)". Inline markers look like
// "[cite: 3, 12]". Turn markers into chips with a hover/tap card, and flag
// paragraphs that make claims without any citation.
const CITE = {
  sources(md) {
    const out = {};
    const tail = md.split(/\n\*\*Sources:?\*\*\s*\n/)[1] || "";
    for (const m of tail.matchAll(/^\s*(\d+)\.\s*\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/gm)) out[m[1]] = { label: m[2], url: m[3] };
    return out;
  },
  decorate(art, md, opts = {}) {
    const src = CITE.sources(md || "");
    if (!Object.keys(src).length) return;
    if (opts.collapseSources) CITE.collapseSources(art);
    const walker = document.createTreeWalker(art, NodeFilter.SHOW_TEXT);
    const hits = []; let n;
    while ((n = walker.nextNode())) if (/\[cite:\s*[\d,\s]+\]/.test(n.nodeValue)) hits.push(n);
    for (const node of hits) {
      const frag = document.createDocumentFragment();
      let last = 0; const s = node.nodeValue;
      for (const m of s.matchAll(/\[cite:\s*([\d,\s]+)\]/g)) {
        frag.append(s.slice(last, m.index));
        const nums = m[1].split(",").map((x) => x.trim()).filter(Boolean);
        const chip = document.createElement("span");
        chip.className = "cite"; chip.dataset.n = nums.join(",");
        chip.textContent = nums.length > 3 ? `${nums.slice(0, 3).join(",")}+` : nums.join(",");
        chip.tabIndex = 0;
        frag.append(chip);
        last = m.index + m[0].length;
      }
      frag.append(s.slice(last));
      node.replaceWith(frag);
    }
    // Uncited-claim markers: substantive paragraphs/list items with no chip.
    const body = opts.noUncited ? [] : [...art.children];
    const stop = body.findIndex((el) => /^sources:?$/i.test(el.textContent.trim()));
    body.slice(0, stop < 0 ? undefined : stop).forEach((el) => {
      if (!/^(P|LI|UL|OL)$/.test(el.tagName)) return;
      const items = el.tagName === "P" ? [el] : [...el.querySelectorAll(":scope > li")];
      for (const it of items) {
        const words = it.textContent.split(/\s+/).length;
        if (words >= 25 && !it.querySelector(".cite") && /\d|[A-Z][a-z]+ [A-Z]/.test(it.textContent)) it.classList.add("uncited");
      }
    });
    document.querySelectorAll(".cite-card").forEach((c) => c.remove());
    const card = document.createElement("div");
    card.className = "cite-card"; card.hidden = true; document.body.appendChild(card);
    const show = (chip) => {
      const nums = chip.dataset.n.split(",");
      const sentence = CITE.sentenceOf(chip);
      card.innerHTML = `<button class="cc-x" title="Close">\u00d7</button><div class="cc-claim">\u201c${esc(clip(sentence, 260))}\u201d</div>` + nums.map((k) => {
        const x = src[k];
        return x ? `<a class="cc-src" href="${esc(x.url)}" target="_blank" rel="noopener noreferrer"><b>[${esc(k)}]</b> ${esc(x.label)} <span class="dim">\u2197</span></a>`
          : `<div class="cc-src dim">[${esc(k)}] not in source list</div>`;
      }).join("");
      card.hidden = false;
      const r = chip.getBoundingClientRect();
      const w = Math.min(360, window.innerWidth - 16);
      card.style.width = w + "px";
      card.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, r.left - w / 2)) + "px";
      const below = r.bottom + 8 + card.offsetHeight < window.innerHeight;
      card.style.top = (below ? r.bottom + 6 : r.top - card.offsetHeight - 6) + "px";
    };
    let hideT;
    art.addEventListener("mouseover", (e) => { const c = e.target.closest(".cite"); if (c) { clearTimeout(hideT); show(c); } });
    art.addEventListener("mouseout", (e) => { if (e.target.closest(".cite")) hideT = setTimeout(() => (card.hidden = true), 350); });
    card.addEventListener("click", (e) => { if (e.target.closest(".cc-x")) card.hidden = true; });
    card.addEventListener("mouseover", () => clearTimeout(hideT));
    card.addEventListener("mouseout", () => (hideT = setTimeout(() => (card.hidden = true), 350)));
    art.addEventListener("click", (e) => { const c = e.target.closest(".cite"); if (c) { e.stopPropagation(); show(c); } });
    document.addEventListener("click", (e) => { if (!e.target.closest(".cite, .cite-card")) card.hidden = true; });
    document.addEventListener("scroll", () => (card.hidden = true), true);
    const unc = art.querySelectorAll(".uncited").length;
    if (unc) art.dataset.uncited = unc;
  },
  // Fold each long "Sources:" list into a <details> so notebooks stay readable.
  collapseSources(art) {
    [...art.querySelectorAll("p, h1, h2, h3, h4")].forEach((el) => {
      if (!/^sources:?$/i.test(el.textContent.trim())) return;
      const list = el.nextElementSibling;
      if (!list || !/^(OL|UL)$/.test(list.tagName)) return;
      const d = document.createElement("details");
      d.className = "src-fold";
      d.innerHTML = `<summary>Sources (${list.children.length})</summary>`;
      el.replaceWith(d); d.appendChild(list);
    });
  },
  sentenceOf(chip) {
    const block = chip.closest("p, li, td") || chip.parentElement;
    const before = document.createRange();
    before.selectNodeContents(block); before.setEndBefore(chip);
    const pre = before.toString();
    const cut = Math.max(pre.lastIndexOf(". ", pre.length - 2), pre.lastIndexOf("? "), pre.lastIndexOf("! "));
    return pre.slice(cut >= 0 ? cut + 2 : 0).replace(/\s*[\d,+]+\s*$/, "").trim();
  },
};

// ---------------------------------------------------------------- read aloud (browser voice, word for word)
const READER = {
  state: null,
  stop() {
    if (!READER.state) return;
    speechSynthesis.cancel();
    READER.state.el?.classList.remove("reading");
    READER.state.bar.hidden = true;
    READER.state = null;
  },
  blocks(art) {
    return [...art.querySelectorAll("h1, h2, h3, h4, p, li, blockquote, td")]
      .filter((el) => !el.closest(".cite-card") && !el.querySelector("p, li") && el.textContent.trim())
      .filter((el) => {
        const t = el.textContent.trim();
        return !/^sources:?$/i.test(t);
      })
      .filter((el, i, all) => {
        const src = all.findIndex((x) => /^sources:?$/i.test(x.textContent.trim()));
        return src < 0 || i < src;
      });
  },
  text(el) {
    const c = el.cloneNode(true);
    c.querySelectorAll(".cite").forEach((x) => x.remove());
    return c.textContent.replace(/\s+/g, " ").trim();
  },
  toggle(v, art, s) {
    if (READER.state) return READER.stop();
    if (!("speechSynthesis" in window)) return toast("This browser has no built-in voice. Use Export \u2192 Audio instead.", "err");
    // stop at the Sources heading
    let blocks = READER.blocks(art);
    const srcIdx = [...art.children].findIndex((el) => /^sources:?$/i.test(el.textContent.trim()));
    if (srcIdx >= 0) { const stopEl = art.children[srcIdx]; blocks = blocks.filter((b) => b.compareDocumentPosition(stopEl) & Node.DOCUMENT_POSITION_FOLLOWING); }
    if (!blocks.length) return toast("Nothing to read", "err");
    const bar = v.querySelector(".listenbar");
    const voices = speechSynthesis.getVoices().filter((x) => /^en/i.test(x.lang));
    const saved = localStorage.getItem("dr.voice");
    bar.innerHTML = `
      <button class="btn small" data-l="prev" title="Previous paragraph">\u23EE</button>
      <button class="btn small primary" data-l="pause">\u23F8 Pause</button>
      <button class="btn small" data-l="next" title="Next paragraph">\u23ED</button>
      <select class="btn small" data-l="rate">${[0.8, 1, 1.15, 1.3, 1.5, 1.75, 2].map((r) => `<option value="${r}" ${r == (localStorage.getItem("dr.rate") || 1) ? "selected" : ""}>${r}\u00d7</option>`).join("")}</select>
      <select class="btn small" data-l="voice" style="max-width:180px">${voices.map((x) => `<option ${x.name === saved ? "selected" : ""}>${esc(x.name)}</option>`).join("") || "<option>default</option>"}</select>
      <span class="mono dim lb-pos"></span>
      <span class="grow"></span>
      <button class="btn small" data-l="stop">\u25A0 Stop</button>`;
    bar.hidden = false;
    // start from the first block visible on screen
    const top = v.getBoundingClientRect().top + 80;
    let idx = Math.max(0, blocks.findIndex((b) => b.getBoundingClientRect().bottom > top));
    const st = (READER.state = { bar, blocks, idx, el: null, paused: false });
    const speak = () => {
      if (READER.state !== st) return;
      if (st.idx >= blocks.length) { toast("Finished reading", "ok"); return READER.stop(); }
      st.el?.classList.remove("reading");
      st.el = blocks[st.idx]; st.el.classList.add("reading");
      st.el.scrollIntoView({ block: "center", behavior: "smooth" });
      bar.querySelector(".lb-pos").textContent = `${st.idx + 1}/${blocks.length}`;
      const u = new SpeechSynthesisUtterance(READER.text(st.el));
      u.rate = +bar.querySelector('[data-l="rate"]').value;
      const vn = bar.querySelector('[data-l="voice"]').value;
      const voice = speechSynthesis.getVoices().find((x) => x.name === vn); if (voice) u.voice = voice;
      u.onend = () => { if (READER.state === st && !st.skip) { st.idx++; speak(); } st.skip = false; };
      speechSynthesis.speak(u);
    };
    const jump = (d) => { st.skip = true; speechSynthesis.cancel(); st.idx = Math.max(0, Math.min(blocks.length - 1, st.idx + d)); st.paused = false; bar.querySelector('[data-l="pause"]').textContent = "\u23F8 Pause"; setTimeout(() => { st.skip = false; speak(); }, 60); };
    bar.onclick = (e) => {
      const b = e.target.closest("[data-l]"); if (!b) return;
      const a = b.dataset.l;
      if (a === "stop") READER.stop();
      else if (a === "prev") jump(-1);
      else if (a === "next") jump(1);
      else if (a === "pause") {
        if (st.paused) { speechSynthesis.resume(); b.textContent = "\u23F8 Pause"; } else { speechSynthesis.pause(); b.textContent = "\u25B6 Resume"; }
        st.paused = !st.paused;
      }
    };
    bar.querySelector('[data-l="rate"]').onchange = (e) => { localStorage.setItem("dr.rate", e.target.value); jump(0); };
    bar.querySelector('[data-l="voice"]').onchange = (e) => { localStorage.setItem("dr.voice", e.target.value); jump(0); };
    // Click any paragraph while reading to jump there.
    art.addEventListener("dblclick", (e) => {
      if (READER.state !== st) return;
      const k = blocks.indexOf(e.target.closest("h1, h2, h3, h4, p, li, blockquote, td"));
      if (k >= 0) { st.idx = k; jump(0); }
    });
    speechSynthesis.cancel();
    speak();
  },
};
if ("speechSynthesis" in window) speechSynthesis.onvoiceschanged = () => {};

// ---------------------------------------------------------------- AI voice audio export
const AUDIO = {
  async exportDialog(kind, id, mode, title) {
    let est;
    try { est = await api("/api/audio/estimate", { method: "POST", body: { kind, id, mode } }); }
    catch (e) { return toast(e.message, "err"); }
    const mins = Math.max(1, Math.round(est.seconds / 60));
    $("#modal").innerHTML = `
      <h3>${mode === "summary" ? "AI voice summary" : "Read aloud: full text"}</h3>
      <div class="dim">${mode === "summary"
        ? "Gemini writes a 2 to 3 minute spoken briefing of this report, then reads it in the voice you pick."
        : `Gemini reads the whole ${kind === "notebook" ? "notebook" : "report"} word for word (citations and links left out). About ${fmtN(est.words)} words, roughly ${mins} min of audio.`}</div>
      <div class="field" style="margin-top:12px"><label>Voice</label>
        <select id="a-voice">${est.voices.map((v) => `<option ${v === (localStorage.getItem("dr.aivoice") || "Charon") ? "selected" : ""}>${v}</option>`).join("")}</select></div>
      <div class="estimate"><span>EST. LENGTH <b>${mode === "summary" ? "2-3" : mins} min</b></span><span>EST. COST <b>$${est.cost_usd.toFixed(2)}</b></span><span class="dim">Gemini 3.8 Flash TTS</span></div>
      <div class="acts"><button class="btn" data-x="0">Cancel</button><button class="btn primary" data-x="1">Create audio</button></div>`;
    $("#modal-back").hidden = false;
    const close = () => ($("#modal-back").hidden = true);
    $('#modal [data-x="0"]').onclick = close;
    $("#modal-back").onclick = (e) => { if (e.target.id === "modal-back") close(); };
    $('#modal [data-x="1"]').onclick = async () => {
      const voice = $("#a-voice").value; localStorage.setItem("dr.aivoice", voice);
      close();
      await AUDIO.run(kind, id, mode, voice, title);
    };
  },
  async run(kind, id, mode, voice, title) {
    status(`creating audio (${mode})\u2026`);
    toast("Creating audio. You can keep reading; it will pop up when ready.");
    try {
      const { job } = await api("/api/audio", { method: "POST", body: { kind, id, mode, voice } });
      let j;
      for (;;) {
        await new Promise((r) => setTimeout(r, 2500));
        j = await api(`/api/audio/jobs/${job}`);
        if (j.status !== "running") break;
      }
      if (j.status === "error") throw new Error(j.error);
      AUDIO.player(j.result, title);
      AUDIO.fillList(kind, id);
      NOTIFY.send("Audio ready", title);
    } catch (e) { toast(`Audio failed: ${e.message}`, "err"); }
    status("ready");
  },
  player(a, title) {
    let p = $("#audio-player");
    if (!p) { p = document.createElement("div"); p.id = "audio-player"; document.body.appendChild(p); }
    const url = `/api/audio/${a.id}/file`;
    p.innerHTML = `<div class="ap-title"><b>${a.mode === "summary" ? "Summary" : "Full report"}</b> \u00b7 ${esc(clip(title || "", 60))} \u00b7 ${esc(a.voice)} \u00b7 ${AUDIO.fmt(a.seconds)}${a.cost_usd != null ? ` \u00b7 $${(+a.cost_usd).toFixed(2)}` : ""}</div>
      <audio controls autoplay preload="auto" src="${url}"></audio>
      <div class="ap-acts"><a class="btn small" href="${url}?download=1" download>Download</a>${a.script ? '<button class="btn small" data-ap="script">Script</button>' : ""}<button class="btn small" data-ap="min" title="Minimize">\u2013</button><button class="btn small" data-ap="x" title="Close">\u00d7</button></div>`;
    p.classList.remove("min");
    p.querySelector('[data-ap="min"]').onclick = (e) => { e.stopPropagation(); p.classList.toggle("min"); };
    p.querySelector(".ap-title").onclick = () => p.classList.remove("min");
    p.hidden = false;
    p.querySelector('[data-ap="x"]').onclick = () => { p.querySelector("audio").pause(); p.hidden = true; };
    p.querySelector('[data-ap="script"]')?.addEventListener("click", () => {
      $("#modal").innerHTML = `<h3>Audio script</h3><div class="md" style="max-height:60vh;overflow:auto;font-size:14px">${esc(a.script).replace(/\n/g, "<br>")}</div>
        <div class="acts"><button class="btn" data-x="c">Copy</button><button class="btn primary" data-x="0">Close</button></div>`;
      $("#modal-back").hidden = false;
      $('#modal [data-x="0"]').onclick = () => ($("#modal-back").hidden = true);
      $('#modal [data-x="c"]').onclick = () => copyText(a.script);
    });
  },
  fmt(sec) { sec = Math.round(sec || 0); return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`; },
  async fillList(kind, id) {
    const el = $("#audio-list"); if (!el) return;
    try {
      const { audio } = await api(`/api/audio?kind=${kind}&id=${id}`);
      el.innerHTML = audio.map((a) => `<a class="src" data-aid="${a.id}"><span>\u25B6 ${a.mode === "summary" ? "Summary" : "Full"} \u00b7 ${esc(a.voice)}</span><i>${AUDIO.fmt(a.seconds)}</i></a>`).join("")
        || `<span>None yet. Export \u2192 Audio.</span>`;
      el.querySelectorAll("[data-aid]").forEach((x) => (x.onclick = () => {
        const a = audio.find((y) => y.id == x.dataset.aid); AUDIO.player(a, S.cache[id]?.prompt || "");
      }));
    } catch { el.textContent = ""; }
  },
};

// ---------------------------------------------------------------- brief builder
const BRIEF = {
  dialog(kind, id, title) {
    $("#modal").innerHTML = `
      <h3>Build a brief</h3>
      <div class="dim">Gemini turns this ${kind === "notebook" ? "notebook" : "report"} into a finished piece, keeping citations attached to each claim. Usually under a cent.</div>
      <div class="templates" style="margin-top:12px">
        <button data-style="brief" class="on">Executive brief</button>
        <button data-style="slides">Slide outline</button>
        <button data-style="email">Email</button>
      </div>
      <div class="acts"><button class="btn" data-x="0">Cancel</button><button class="btn primary" data-x="1">Build</button></div>`;
    $("#modal-back").hidden = false;
    let style = "brief";
    $$("#modal [data-style]").forEach((b) => (b.onclick = () => { style = b.dataset.style; $$("#modal [data-style]").forEach((x) => x.classList.toggle("on", x === b)); }));
    const close = () => ($("#modal-back").hidden = true);
    $('#modal [data-x="0"]').onclick = close;
    $('#modal [data-x="1"]').onclick = async () => {
      const btn = $('#modal [data-x="1"]'); btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> Building';
      try {
        const r = await api("/api/brief", { method: "POST", body: { kind, id, style } });
        close();
        const label = { brief: "Brief", slides: "Slides", email: "Email" }[style];
        const src = kind === "session" ? `\n\n---\n*Built from Session #${id}*\n` : "";
        await NB.create(`${label}: ${clip(title, 60)}`, r.markdown + src);
        toast(`${label} ready as a new notebook${r.cost_usd != null ? ` ($${r.cost_usd.toFixed(3)})` : ""}`, "ok");
      } catch (e) { toast(e.message, "err"); btn.disabled = false; btn.textContent = "Build"; }
    };
  },
};

// ---------------------------------------------------------------- actual cost
const COST = {
  async fill(s) {
    const el = $("#cost-v"); if (!el) return;
    try {
      const r = await api(`/api/sessions/${s.id}/usage`);
      if (!$("#cost-v")) return;
      if (!r.usage) { el.innerHTML = `<span class="dim">${esc(r.error || "unknown")}</span>`; return; }
      const u = r.usage;
      el.innerHTML = `<b>$${u.model_usd.toFixed(2)}</b>${r.estimate_usd != null ? ` <span class="dim">est. $${r.estimate_usd.toFixed(2)}</span>` : ""}`;
      $("#cost-detail").innerHTML = `${fmtN(u.input_tokens)} in (${fmtN(u.cached_tokens)} cached) \u00b7 ${fmtN(u.output_tokens + u.thought_tokens)} out \u00b7 ${u.searches} searches${u.searches ? ` (+$${u.search_usd_if_over_free.toFixed(2)} only past 5,000/mo free)` : ""}`;
    } catch (e) { el.innerHTML = `<span class="dim">${esc(e.message)}</span>`; }
  },
};

// ---------------------------------------------------------------- live timeline
const TIMELINE = {
  timer: null, sid: null, finished: {},
  stop() { clearTimeout(TIMELINE.timer); TIMELINE.sid = null; },
  start(s) {
    TIMELINE.stop(); TIMELINE.sid = s.id;
    const tick = async () => {
      if (TIMELINE.sid !== s.id || !$("#timeline")) return;
      try { TIMELINE.draw(await api(`/api/sessions/${s.id}/timeline`), s); } catch { /* transient */ }
      const running = S.sessions.find((x) => x.id === s.id)?.status === "running";
      TIMELINE.timer = setTimeout(tick, running ? 3000 : 30000);
    };
    tick();
  },
  draw(t, s) {
    const el = $("#timeline"); if (!el) return;
    const t0 = new Date(t.lanes[0].created_at).getTime();
    const running = t.status === "running";
    let end = running ? Date.now() : Math.max(...t.lanes.map((l) => new Date(l.updated_at).getTime()));
    if (!running && TIMELINE.finished[s.id]) end = TIMELINE.finished[s.id];
    const span = Math.max(end - t0, 1000);
    const el_s = Math.round(span / 1000);
    // Finished runs launched before v0.17 have no trustworthy end time (indexing
    // used to bump updated_at), so only show elapsed when we know it.
    const known = running || !!t.run || !!TIMELINE.finished[s.id];
    const est = t.run?.estimate_usd;
    const thoughts = t.events.filter((e) => e.kind === "thought");
    const errors = t.events.filter((e) => e.kind === "error");
    const sources = extractSources(S.cache[s.id]?.result || "").length;
    el.innerHTML = `
      <div class="tl-head">
        <div><div class="label">Elapsed</div><div class="tl-big">${known ? `${Math.floor(el_s / 60)}m ${String(el_s % 60).padStart(2, "0")}s` : "\u2014"}</div></div>
        <div><div class="label">Agents</div><div class="tl-big">${t.lanes.length}</div></div>
        <div><div class="label">Sources</div><div class="tl-big">${running ? "\u2026" : sources}</div></div>
        <div><div class="label">Cost</div><div class="tl-big" id="tl-cost">${est != null ? `<span class="dim" style="font-size:12px">est</span> $${est.toFixed(2)}` : "\u2014"}</div></div>
      </div>
      ${running && est != null ? `<div class="tl-bar"><i style="width:${Math.min(100, (span / 1000 / 60 / (20 * Math.max(1, t.lanes.length))) * 100)}%"></i></div><div class="dim mono" style="font-size:10px;margin:-4px 0 10px">Deep Research runs usually finish in 5 to 20 minutes per agent.</div>` : ""}
      <div class="tl-lanes">${t.lanes.map((l) => {
        const a = new Date(l.created_at).getTime(), b = l.status === "running" ? Date.now() : new Date(l.updated_at).getTime();
        const left = ((a - t0) / span) * 100, w = Math.max(1.5, ((b - a) / span) * 100);
        return `<div class="tl-lane" data-id="${l.id}" title="#${l.id} ${esc(l.prompt)}">
          <span class="tl-lbl">${"\u00a0".repeat((l.depth - 1) * 2)}#${l.id}</span>
          <span class="tl-track"><i class="st-${esc(l.status)}" style="left:${left}%;width:${w}%"></i></span></div>`;
      }).join("")}</div>
      <div class="label" style="margin:12px 0 6px">Agent thinking${thoughts.length ? ` (${thoughts.length})` : ""}</div>
      <div class="tl-events">${thoughts.slice(-40).reverse().map((e) => `<div class="tl-ev"><span class="mono dim">${esc(e.t || "")}</span> ${esc(e.text)}</div>`).join("")
        || `<div class="dim">${running ? "Waiting for the agent's first thoughts\u2026" : "No thought log for this run (older runs and terminal runs don't keep one)."}</div>`}</div>
      ${errors.length ? `<div class="label" style="margin:12px 0 6px;color:var(--red)">Errors</div>${errors.map((e) => `<div class="tl-ev er">${esc(e.text)}</div>`).join("")}` : ""}`;
    el.querySelectorAll(".tl-lane").forEach((x) => (x.onclick = () => openSession(x.dataset.id)));
    if (!running) api(`/api/sessions/${s.id}/usage`).then((r) => {
      // Google's own finish time beats our updated_at (which older versions bumped on indexing).
      const fin = r.usage?.finished ? new Date(r.usage.finished).getTime() : null;
      if (fin && fin > t0 && TIMELINE.finished[s.id] !== fin) { TIMELINE.finished[s.id] = fin; return TIMELINE.draw(t, s); }
      const c = $("#tl-cost"); if (c && r.usage) c.innerHTML = `$${r.usage.model_usd.toFixed(2)}${est != null ? ` <span class="dim" style="font-size:11px">est $${est.toFixed(2)}</span>` : ""}`;
    }).catch(() => {});
  },
};

// ---------------------------------------------------------------- notifications
const NOTIFY = {
  ask() { if ("Notification" in window && Notification.permission === "default") Notification.requestPermission().catch(() => {}); },
  send(title, body) {
    if ("Notification" in window && Notification.permission === "granted" && document.hidden) {
      try { new Notification(title, { body: clip(body, 140), tag: "dr" }); } catch { /* ignore */ }
    }
  },
  diff(before, after) {
    if (!before.length) return;
    const old = new Map(before.map((s) => [s.id, s.status]));
    for (const s of after) {
      if (old.get(s.id) === "running" && s.status !== "running" && !s.parent_id) {
        NOTIFY.send(`Research #${s.id} ${s.status}`, s.prompt);
        toast(`Research #${s.id} ${s.status}`, s.status === "completed" ? "ok" : "err");
      }
    }
  },
};

// ---------------------------------------------------------------- research map
async function renderMap(v) {
  v.innerHTML = `<div class="vbar"><span class="title">Research map</span><span class="mono dim" id="map-info"></span><span class="grow"></span>
    <input class="inline-input" id="map-q" placeholder="Highlight\u2026" style="max-width:200px">
    <button class="btn small" id="map-fit">Fit</button></div>
    <div class="map-wrap"><canvas id="map-c"></canvas><div class="map-tip" hidden></div></div>`;
  let data;
  try { data = await api("/api/map"); } catch (e) { v.querySelector(".map-wrap").innerHTML = `<div class="empty-result">${esc(e.message)}</div>`; return; }
  if (!document.body.contains(v)) return;  // tab closed while loading
  if (!data.nodes.length) { v.querySelector(".map-wrap").innerHTML = `<div class="empty-result">No indexed research yet. Run a semantic search once to index your reports.</div>`; return; }
  v.querySelector("#map-info").textContent = `${data.nodes.length} reports \u00b7 ${data.edges.length} links \u00b7 closer = more similar`;
  const canvas = $("#map-c"), wrap = v.querySelector(".map-wrap"), tip = v.querySelector(".map-tip");
  const ctx = canvas.getContext("2d");
  const N = data.nodes, byId = new Map(N.map((n, i) => [n.id, i]));
  const E = data.edges.map((e) => [byId.get(e.a), byId.get(e.b), e.sim]).filter((e) => e[0] != null && e[1] != null);
  // normalize PCA seed, then relax with a small force layout
  const xs = N.map((n) => n.x), ys = N.map((n) => n.y);
  const nx = (x) => (x - Math.min(...xs)) / ((Math.max(...xs) - Math.min(...xs)) || 1) - 0.5;
  const ny = (y) => (y - Math.min(...ys)) / ((Math.max(...ys) - Math.min(...ys)) || 1) - 0.5;
  const P = N.map((n) => ({ x: nx(n.x) * 2, y: ny(n.y) * 2, vx: 0, vy: 0 }));
  for (let it = 0; it < 260; it++) {
    const k = 0.02 * (1 - it / 260);
    for (let i = 0; i < P.length; i++) for (let j = i + 1; j < P.length; j++) {
      const dx = P[i].x - P[j].x, dy = P[i].y - P[j].y, d2 = dx * dx + dy * dy + 1e-4;
      const f = 0.0009 / d2; P[i].vx += dx * f; P[i].vy += dy * f; P[j].vx -= dx * f; P[j].vy -= dy * f;
    }
    for (const [a, b, s] of E) {
      const dx = P[b].x - P[a].x, dy = P[b].y - P[a].y, d = Math.sqrt(dx * dx + dy * dy) || 1e-3;
      const target = 0.12 + (1 - s) * 0.9, f = (d - target) * 0.05 * s;
      P[a].vx += (dx / d) * f; P[a].vy += (dy / d) * f; P[b].vx -= (dx / d) * f; P[b].vy -= (dy / d) * f;
    }
    for (const p of P) { p.vx -= p.x * 0.004; p.vy -= p.y * 0.004; p.x += p.vx * k * 40; p.y += p.vy * k * 40; p.vx *= 0.6; p.vy *= 0.6; }
  }
  // clusters: connected components of strong links
  const comp = N.map((_, i) => i);
  const find = (i) => (comp[i] === i ? i : (comp[i] = find(comp[i])));
  for (const [a, b, s] of E) if (s >= 0.8) comp[find(a)] = find(b);
  const palette = ["#22d3ee", "#f5a524", "#e879f9", "#34d399", "#f87171", "#a78bfa", "#60a5fa", "#fbbf24", "#2dd4bf", "#fb7185"];
  const sizes = {}; N.forEach((_, i) => (sizes[find(i)] = (sizes[find(i)] || 0) + 1));
  const ranked = Object.keys(sizes).sort((a, b) => sizes[b] - sizes[a]);
  const color = (i) => { const r = ranked.indexOf(String(find(i))); return sizes[find(i)] > 1 && r < palette.length ? palette[r] : "#5b6679"; };
  let view = { s: 1, x: 0, y: 0 }, hover = -1, q = "";
  const fit = () => {
    const W = wrap.clientWidth, H = wrap.clientHeight;
    const minx = Math.min(...P.map((p) => p.x)), maxx = Math.max(...P.map((p) => p.x)), miny = Math.min(...P.map((p) => p.y)), maxy = Math.max(...P.map((p) => p.y));
    view.s = Math.min(W / ((maxx - minx) || 1), H / ((maxy - miny) || 1)) * 0.86;
    view.x = W / 2 - ((minx + maxx) / 2) * view.s; view.y = H / 2 - ((miny + maxy) / 2) * view.s;
  };
  const sx = (p) => p.x * view.s + view.x, sy = (p) => p.y * view.s + view.y;
  const rad = (i) => 3 + Math.min(7, Math.sqrt((N[i].chars || 0) / 4000));
  const draw = () => {
    const dpr = window.devicePixelRatio || 1, W = wrap.clientWidth, H = wrap.clientHeight;
    canvas.width = W * dpr; canvas.height = H * dpr; canvas.style.width = W + "px"; canvas.style.height = H + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H);
    const match = (i) => q && N[i].prompt.toLowerCase().includes(q);
    for (const [a, b, s] of E) {
      const on = hover === a || hover === b;
      ctx.strokeStyle = on ? "rgba(34,211,238,.55)" : `rgba(120,140,170,${(s - 0.7) * 0.5})`;
      ctx.lineWidth = on ? 1.4 : 0.8;
      ctx.beginPath(); ctx.moveTo(sx(P[a]), sy(P[a])); ctx.lineTo(sx(P[b]), sy(P[b])); ctx.stroke();
    }
    N.forEach((n, i) => {
      const dim = q && !match(i);
      ctx.globalAlpha = dim ? 0.18 : 1;
      ctx.fillStyle = color(i);
      ctx.beginPath(); ctx.arc(sx(P[i]), sy(P[i]), rad(i) + (i === hover ? 2 : 0), 0, Math.PI * 2); ctx.fill();
      if (match(i) || i === hover) { ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.5; ctx.stroke(); }
    });
    ctx.globalAlpha = 1;
    if (view.s > 500 || q) {
      ctx.font = "11px Inter, sans-serif"; ctx.fillStyle = "#c9d4e5";
      N.forEach((n, i) => { if (!q || match(i)) ctx.fillText(clip(n.prompt, 38), sx(P[i]) + rad(i) + 4, sy(P[i]) + 4); });
    }
  };
  const pick = (mx, my) => { let best = -1, bd = 1e9; P.forEach((p, i) => { const d = (sx(p) - mx) ** 2 + (sy(p) - my) ** 2; if (d < bd && d < (rad(i) + 6) ** 2) { bd = d; best = i; } }); return best; };
  const pos = (e) => { const r = canvas.getBoundingClientRect(); const t = e.touches?.[0] || e; return [t.clientX - r.left, t.clientY - r.top]; };
  let drag = null, moved = false;
  canvas.onpointerdown = (e) => { drag = pos(e); moved = false; canvas.setPointerCapture(e.pointerId); };
  canvas.onpointermove = (e) => {
    const [mx, my] = pos(e);
    if (drag) { const dx = mx - drag[0], dy = my - drag[1]; if (Math.abs(dx) + Math.abs(dy) > 3) moved = true; view.x += dx; view.y += dy; drag = [mx, my]; draw(); return; }
    const h = pick(mx, my);
    if (h !== hover) { hover = h; draw(); }
    if (h >= 0) {
      const n = N[h];
      tip.innerHTML = `<b>#${n.id}</b> <span class="dim">${ago(n.created_at)}</span><br>${esc(clip(n.prompt, 180))}`;
      tip.hidden = false; tip.style.left = Math.min(mx + 14, wrap.clientWidth - 280) + "px"; tip.style.top = my + 14 + "px";
    } else tip.hidden = true;
  };
  canvas.onpointerup = (e) => { const [mx, my] = pos(e); drag = null; if (!moved) { const h = pick(mx, my); if (h >= 0) openSession(N[h].id); } };
  canvas.onwheel = (e) => {
    e.preventDefault(); const [mx, my] = pos(e); const f = Math.exp(-e.deltaY * 0.0015);
    view.x = mx - (mx - view.x) * f; view.y = my - (my - view.y) * f; view.s *= f; draw();
  };
  $("#map-fit").onclick = () => { fit(); draw(); };
  $("#map-q").oninput = debounce((e) => { q = e.target.value.trim().toLowerCase(); draw(); }, 120);
  new ResizeObserver(() => { if (document.body.contains(canvas)) draw(); }).observe(wrap);
  fit(); draw();
}

// ---------------------------------------------------------------- compare
async function renderCompare(v, t) {
  v.innerHTML = `<div class="pad"><div class="empty-result scan">Loading #${t.a} and #${t.b}\u2026</div></div>`;
  let A, B;
  try { [A, B] = await Promise.all([loadSession(t.a, true), loadSession(t.b, true)]); }
  catch (e) { v.innerHTML = `<div class="pad"><div class="empty-result">${esc(e.message)}</div></div>`; return; }
  let diff;
  try { diff = await api("/api/compare", { method: "POST", body: { a: t.a, b: t.b } }); } catch (e) { diff = null; }
  const srcList = (arr, cls) => {
    if (!arr.length) return '<span class="dim">none</span>';
    const pill = (x) => `<span class="tagpill ${cls}">${esc(x)}</span>`;
    return arr.length <= 12 ? arr.map(pill).join("")
      : arr.slice(0, 12).map(pill).join("") + `<details><summary>+${arr.length - 12} more</summary><div class="v">${arr.slice(12).map(pill).join("")}</div></details>`;
  };
  v.innerHTML = `
    <div class="vbar"><span class="title">Compare #${t.a} (older) \u21C4 #${t.b} (newer)</span><span class="grow"></span>
      <button class="btn small primary" id="cmp-sum">What changed? (AI)</button></div>
    <div class="pad" style="padding-bottom:10px">
      <div class="card cmp-src" style="margin-bottom:12px"><h3>Sources</h3>
        <div class="kv"><span class="k">only in #${t.a}</span><span class="v">${srcList(diff?.sources_only_a || [], "gone")}</span>
        <span class="k">new in #${t.b}</span><span class="v">${srcList(diff?.sources_only_b || [], "new")}</span>
        <span class="k">in both</span><span class="v">${srcList(diff?.sources_shared || [], "")}</span></div></div>
      <div class="card" id="cmp-out" hidden><h3>What changed</h3><div class="md" id="cmp-md"></div></div>
    </div>
    <div class="cmp">
      <div class="cmp-col"><div class="label">#${A.id} \u00b7 ${esc(niceTime(A.created_at))}</div><article class="md">${renderMd(A.result || "")}</article></div>
      <div class="cmp-col"><div class="label">#${B.id} \u00b7 ${esc(niceTime(B.created_at))}</div><article class="md">${renderMd(B.result || "")}</article></div>
    </div>`;
  const cols = v.querySelectorAll(".cmp-col");
  // new paragraphs in B (no near-identical paragraph in A) get a green edge
  const norm = (s) => s.toLowerCase().replace(/\[cite[^\]]*\]/g, "").replace(/[^a-z0-9 ]/g, "").replace(/\s+/g, " ").trim();
  const aSet = new Set([...cols[0].querySelectorAll("p, li")].map((p) => norm(p.textContent)));
  cols[1].querySelectorAll("p, li").forEach((p) => { if (p.textContent.split(/\s+/).length > 8 && !aSet.has(norm(p.textContent))) p.classList.add("is-new"); });
  cols.forEach((c) => c.querySelectorAll("a[href^='http']").forEach((a) => { a.target = "_blank"; a.rel = "noopener noreferrer"; }));
  $("#cmp-sum").onclick = async () => {
    const b = $("#cmp-sum"); b.disabled = true; b.innerHTML = '<span class="spinner"></span> Comparing';
    try {
      const r = await api("/api/compare", { method: "POST", body: { a: t.a, b: t.b, summarize: true } });
      $("#cmp-out").hidden = false; $("#cmp-md").innerHTML = renderMd(r.summary || "");
      b.textContent = "Done";
    } catch (e) { toast(e.message, "err"); b.disabled = false; b.textContent = "What changed? (AI)"; }
  };
}
