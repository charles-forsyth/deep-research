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

## Next: five improvements

1. **Citation hover cards.** Hovering or tapping `[cite: 34]` shows the source title, domain and
   the passage that supports the claim, with a link out. Uncited paragraphs get a faint margin
   mark so unsupported claims are visible at a glance.
2. **Research map.** A zoomable graph of the whole archive: sessions as nodes, linked by shared
   sources and topic similarity (from the existing embeddings). Clusters show what you already
   know; clicking a node opens the report.
3. **Re-run and compare.** One click re-runs an old question. The two reports are shown side by
   side with what changed highlighted: new findings, claims that no longer hold, new sources.
   This turns one-off research into tracked topics.
4. **Live run timeline.** Replace the raw log with a visual timeline: each sub-task as a lane,
   sources appearing as they are found, a running elapsed time and cost against the estimate, and
   a browser notification when the run finishes.
5. **Brief builder.** Turn a notebook into a finished product: one-page executive brief, slide
   outline, or email, with every quote keeping its citation back to the source session. Optional
   read-aloud audio version of any report or brief for listening on the go.
