# Mission Control TUI Implementation Checklist

## 1. Project Setup
- [ ] Create virtual environment and install `textual` and `textual-dev`.
- [ ] Create file structure:
    - `main.py` (Entry point)
    - `mission_control.tcss` (Stylesheet)
    - `widgets.py` (Custom widget classes)
    - `logger.py` (Custom logging handler)

## 2. Layout & CSS (The "Mission Control" Grid)
- [ ] Define CSS variables in `mission_control.tcss` (Root scope):
    -  `$surface-bg: #1d1f21;`
    -  `$panel-bg: #2b211e;`
    -  `$accent-gold: #ded398;`
    -  `$text-amber: #ffb000;`
- [ ] Implement Grid Layout in TCSS:
    -  2 Columns (Sidebar fixed, Main flex).
    -  2 Rows (Report flex 2, Log flex 1).
- [ ] Define `Panel` class in TCSS with `border: double $accent-gold;`.

## 3. Core Components (Python)
- [ ] **Main App:**
    -  Subclass `App`.
    -  Set `CSS_PATH = "mission_control.tcss"`.
    -  Implement `compose()` yielding `Header`, `Footer`, and 3 Containers.
- [ ] **Log Pane:**
    -  Create `class MissionLog(Log):`.
    -  Implement `logging.Handler` subclass that writes to `MissionLog`.
    -  Integrate with Python's `logging` module in `on_mount()`.
- [ ] **History Pane:**
    -  Create `class MissionHistory(DataTable):`.
    -  Configure `cursor_type="row"`.
    -  Add columns ("ID", "Timestamp", "Status").
- [ ] **Report Pane:**
    -  Create `class MissionReport(DataTable):`.
    -  Add method `load_mission_data(mission_id)` to clear and repopulate rows.

## 4. Interaction & Logic
- [ ] **Event Wiring:**
    -  Implement `on_data_table_row_selected` in Main App.
    -  Logic: Get `row_key` from History -> Call `MissionReport.load_mission_data`.
- [ ] **Key Bindings:**
    -  Bind `q` to Quit.
    -  Bind `l` to Toggle Log visibility (optional layout shift).
    -  Bind `Tab` to cycle focus between panes.

## 5. Aesthetic Polish (Sci-Fi/Victorian)
- [ ] Apply `box.DOUBLE` or `box.HEAVY` to widget borders.
- [ ] Set `Header(show_clock=True)`.
- [ ] Style `Log` text to be `$text-amber` (Monochrome monitor look).
- [ ] Ensure `Header` and `Footer` colors match the `$panel-bg` theme.

## 6. Testing
- [ ] Run `textual console` for debugging.
- [ ] Verify layout responsiveness on different terminal sizes.
