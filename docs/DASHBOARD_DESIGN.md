# Deep Research Workstation: design

A private research desk in the browser: launch AI deep research, watch it work, and turn the
results into your own knowledge base.

## Core idea

Each report the CLI produced used to be read once and forgotten. The dashboard treats the whole
research history as a library you keep building on:

ask -> watch -> read -> mark up -> collect -> reuse

## What it gives people

1. **Launch without the command line.** A question or a template (market scan, literature
   review, tech deep dive, due diligence, policy brief, compare), depth and breadth controls, a
   cost estimate before you commit, and your own documents searched first.
2. **Watch it think.** A live log of the agent's progress, a tree of sub-tasks as deeper runs
   branch, and cancel (local process and cloud).
3. **Read like an analyst.** Clean documents with an outline, find-in-page, sources grouped by
   domain with citation counts, and follow-up questions on the report in front of you.
4. **Mark it up.** Four highlight colors and notes, saved with the report.
5. **Collect into notebooks.** Send a passage to a Markdown notebook as a cited quote;
   notebooks autosave.
6. **Find anything.** The full archive can be filtered, starred and tagged; semantic search
   across every past report writes a cited answer from your own research.
7. **Take it out.** Export Markdown (optionally with annotations and all sub-reports), HTML,
   JSON, print or PDF.

## Principles

- **An instrument panel, not a chatbot.** A dark analyst-workstation look, with live counters for
  running, complete, failed, corpus size and API key health.
- **Three panes.** Archive left, work center (tabs), inspector right (intel, notes, outline, live
  log). On phones the side panes are slide-out drawers.
- **Keyboard-first.** Ctrl K command palette, Ctrl F find, Ctrl S save.
- **Private and self-hosted.** Binds 0.0.0.0:7420 on the owner's machine and is reached over the
  home LAN and tailnet only. No cloud account; research lives in the local SQLite history.
- **No build step.** Standard-library Python server, vanilla JS, vendored marked + DOMPurify.
  Launches use the same detached `--adopt-session` path as the CLI, so the two never drift.
- **Honest status.** Runs whose process died show as crashed, never "running" forever.

## Shipped in v0.17

1. **Citation cards.** `[cite: 34]` markers become chips; hover or tap shows the claim and the
   numbered source it points to, with a link out. Paragraphs that contain numbers or names but no
   citation get an amber edge. (The reports do not store the passage on the source page, so the
   card shows the source and the sentence it supports.)
2. **Research map.** Every indexed report as a dot, placed by embedding similarity and linked to
   its closest neighbours; clusters are coloured. Pan, zoom, highlight by keyword, click to open.
3. **Re-run and compare.** One click re-runs a question (with a cost estimate first) and links the
   two runs. Compare shows sources dropped/added, marks new paragraphs in green, and can ask Gemini
   for a "what changed" summary.
4. **Live timeline.** Replaces the raw log (still available underneath): elapsed time, agent
   lanes, sources, the agent's thought stream with timestamps, errors, and a browser notification
   when a run finishes. **Actual cost** comes from Google's own token usage for the run and sits
   next to the estimate.
5. **Brief builder.** A report or notebook becomes an executive brief, slide outline or email,
   saved as a new notebook with citations kept.
6. **Read aloud.** "Listen" reads the report word for word in the browser's own voice (free,
   offline), highlighting each paragraph, with speed, voice, skip and pause.
7. **Audio export.** Export -> Audio makes an MP3 with a Gemini voice (8 voices): the full text
   word for word, or an AI-written 2-3 minute spoken summary. Cost is shown before you create it;
   audio is cached and listed on the report.

Estimates were recalibrated in v0.17: the old model assumed 60k in / 4k out per agent run, about
10x below real runs. It now uses Google's published figures (~250k in, ~60% cached, ~60k out),
which match measured runs.
