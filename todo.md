# Mission Control TUI Implementation Checklist

## 1. Project Setup
- [ ] Create `tui/` directory structure:
    - `tui/__init__.py`
    - `tui/app.py` (Main Entry Point)
    - `tui/styles.tcss` (Stylesheet)
    - `tui/widgets.py` (Custom widget classes)
- [ ] Add `textual` to `pyproject.toml` dependencies.

## 2. Engine Refactoring (Preparation)
- [ ] Update `deep_research.py`: Modify `DeepResearchAgent` methods to accept a `logger_callback` (optional) to pipe output to TUI.
- [ ] Ensure `SessionManager` is importable and usable from `tui/app.py`.

## 3. UI Layout & Styling ("Aether-Punk")
- [ ] Define CSS variables in `styles.tcss`:
    - `$surface-bg: #1d1f21;` (Deep Charcoal)
    - `$panel-bg: #2b211e;` (Raisin Black)
    - `$accent-gold: #ded398;` (Tarnished Gold)
    - `$text-amber: #ffb000;` (CRT Amber)
- [ ] Implement Grid Layout:
    - Left Sidebar (History): Fixed width (30-40 chars).
    - Main Area: Split Vertical (Top: Report, Bottom: Log).
    - Footer: Input Bar + Checkbox.

## 4. Core Components (Widgets)
- [ ] **History Pane:** `DataTable` with columns: ID, Status (Icon), Prompt. Auto-refresh (poll DB) every 2s.
- [ ] **Log Pane:** `RichLog` widget to display the "Thinking..." stream.
- [ ] **Report Pane:** `Markdown` widget to render the final result (or live stream).
- [ ] **Input Zone:** `Input` widget for prompt + `Checkbox` ("Run in Background").

## 5. Interaction Logic
- [ ] **Row Selection:** Clicking a row in History queries `SessionManager` and loads the Report markdown into the Report Pane.
- [ ] **Submission (Live):** Enter -> Run `DeepResearchAgent` in a Worker Thread. Stream output to Log Pane via callback.
- [ ] **Submission (Background):** Enter (w/ Checkbox) -> Call `detach_process` (Headless). Add "Pending" row to History immediately.
- [ ] **Polling:** Background timer updates History icons (Running -> Done).

## 6. Aesthetic Polish
- [ ] Apply `box.DOUBLE` or `box.HEAVY` borders with `$accent-gold`.
- [ ] Header: "MISSION CONTROL // DEEP RESEARCH".
- [ ] Status Indicators: Green Dot (Running), Blue Dot (Done), Red (Fail).

## 7. Final Polish
- [ ] Add `tui` command to `deep_research.py` CLI (`deep-research tui`).