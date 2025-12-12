from textual.widgets import DataTable, Log, Static, Input, Checkbox
from textual.containers import Vertical, Horizontal
from textual.app import ComposeResult

class MissionHistory(DataTable):
    """Left sidebar showing past sessions."""
    def on_mount(self):
        self.cursor_type = "row"
        self.add_columns("ID", "Status", "Prompt")
        self.border_title = "MISSION LOGS"

class MissionReport(Static):
    """Top right pane showing the final report markdown."""
    def on_mount(self):
        self.border_title = "DATA UPLINK"
        self.update("Select a mission to view data.")

class MissionLog(Log):
    """Bottom right pane showing live telemetry."""
    def on_mount(self):
        self.border_title = "TELEMETRY"

class InputArea(Horizontal):
    """Bottom docked area for input and controls."""
    def compose(self) -> ComposeResult:
        yield Input(placeholder="Enter Research Objective...", id="prompt-input", classes="input-box")
        yield Checkbox("Background Mode (Headless)", id="bg-mode")
