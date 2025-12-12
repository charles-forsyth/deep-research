import sys
import os

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Checkbox, DataTable, Input
from textual.worker import Worker, get_current_worker
from textual import work

from deep_research import DeepResearchAgent, SessionManager, ResearchRequest, detach_process, xdg_config_home
from tui.widgets import MissionHistory, MissionReport, MissionLog, InputArea

class MissionControlApp(App):
    CSS_PATH = "styles.tcss"
    TITLE = "MISSION CONTROL // DEEP RESEARCH"
    
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield MissionHistory(id="history-pane")
        yield MissionReport(id="report-pane")
        yield MissionLog(id="log-pane")
        yield InputArea(id="input-dock")
        yield Footer()

    def on_mount(self):
        self.session_manager = SessionManager()
        self.refresh_history()
        self.set_interval(2.0, self.refresh_history) # Poll for background task updates

    def refresh_history(self):
        table = self.query_one(MissionHistory)
        
        # Save selection
        # selected_row = table.cursor_row
        
        sessions = self.session_manager.list_sessions(limit=50)
        table.clear()
        for s in sessions:
            if s['status'] == 'running':
                status_icon = "🟢" 
            elif s['status'] == 'completed':
                status_icon = "🔵"
            elif s['status'] == 'failed':
                status_icon = "🔴"
            else:
                status_icon = "⚪"
                
            table.add_row(str(s['id']), status_icon, s['prompt'].replace('\n', ' '), key=str(s['id']))
            
        # Restore selection? Textual clears keys on clear().
        # Maybe we only update if count changed?
        # For MVP, full refresh is okay, but selection might be lost.

    def on_data_table_row_selected(self, event):
        row_key = event.row_key.value
        session = self.session_manager.get_session(row_key)
        report_pane = self.query_one(MissionReport)
        
        if session and session['result']:
            # Render Markdown
            report_pane.update(session['result'])
        else:
            if session and session['status'] == 'running':
                report_pane.update("[italic]Mission in progress... Stand by for data uplink.[/]")
            else:
                report_pane.update("[italic]No report data available.[/]")

    def on_input_submitted(self, event):
        prompt = event.value
        if not prompt: return
        
        is_bg = self.query_one("#bg-mode", Checkbox).value
        event.input.value = "" # Clear input
        
        if is_bg:
            self.launch_headless(prompt)
        else:
            self.run_live_research(prompt)

    def launch_headless(self, prompt):
        sid = self.session_manager.create_session("pending_tui", prompt)
        log_file = os.path.join(xdg_config_home, "deepresearch", "logs", f"session_{sid}.log")
        
        child_args = ["research", prompt, "--adopt-session", str(sid)]
        detach_process(child_args, log_file)
        
        self.query_one(MissionLog).write(f"[bold green]Mission #{sid} launched in background.[/]\n")
        self.refresh_history()

    @work(thread=True)
    def run_live_research(self, prompt):
        log_pane = self.query_one(MissionLog)
        
        # Create session
        sid = self.session_manager.create_session("pending_live", prompt)
        
        self.app.call_from_thread(log_pane.write, f"[bold cyan]Initializing Mission #{sid}...[/]\n")
        self.app.call_from_thread(self.refresh_history)
        
        def tui_logger(msg):
            # Textual Log.write adds newline automatically? No, write() doesn't usually.
            # But deep_research sends chunks.
            # We want to buffer?
            # Textual's Log widget is essentially a scrollable text area.
            # write() appends content.
            self.app.call_from_thread(log_pane.write, msg) 
        
        req = ResearchRequest(
            prompt=prompt,
            stream=True,
            adopt_session_id=sid
        )
        
        # Run agent
        agent = DeepResearchAgent(logger=tui_logger)
        agent.start_research_stream(req)
        
        self.app.call_from_thread(self.refresh_history)
        self.app.call_from_thread(log_pane.write, f"\n[bold blue]Mission #{sid} Complete.[/]\n")

if __name__ == "__main__":
    app = MissionControlApp()
    app.run()
