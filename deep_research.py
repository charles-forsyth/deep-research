#!/usr/bin/env python3
import sys
import os
import pathlib

# --- VIRTUAL ENVIRONMENT BOOTSTRAP ---
# Robustly resolve the directory of the actual script (resolving symlinks)
script_path = pathlib.Path(__file__).resolve()
project_root = script_path.parent
venv_python = project_root / ".venv" / "bin" / "python"

# If running with system python (prefix match) and local venv exists
if sys.prefix == sys.base_prefix and venv_python.exists():
    try:
        # Re-execute using the venv python
        # We use str(venv_python) for compatibility
        os.execv(str(venv_python), [str(venv_python)] + sys.argv)
    except OSError as e:
        print(f"[WARN] Failed to bootstrap virtual environment: {e}")
# -------------------------------------

import time
import argparse
import json
import re
import sqlite3
import subprocess
import concurrent.futures
import warnings
import logging

# Suppress library noise for clean CLI output
warnings.filterwarnings("ignore", category=UserWarning, module="google.genai")
logging.getLogger("google_genai").setLevel(logging.ERROR)

from datetime import datetime
from importlib.metadata import version, PackageNotFoundError
from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel, Field, ValidationError, field_validator
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.panel import Panel
from rich.tree import Tree
from rich.prompt import Prompt
from rich.terminal_theme import MONOKAI

# Load environment variables
load_dotenv()

# Initialize Rich Console
# Force width=120 to prevent layout crashes in headless (non-TTY) mode
console = Console(width=120)
xdg_config_home = os.getenv("XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config"))
user_config_path = os.path.join(xdg_config_home, "deepresearch", ".env")
user_db_path = os.path.join(xdg_config_home, "deepresearch", "history.db")
load_dotenv(user_config_path)

# Fallback version if not installed as a package
__version__ = "0.13.1"

def get_version():
    try:
        return version("deepresearch")
    except PackageNotFoundError:
        return __version__

def detach_process(args_list: list[str], log_path: str) -> int:
    """
    Spawns a detached subprocess that survives terminal closure.
    Redirects stdout/stderr to the specified log file.
    Returns the PID of the spawned process.
    """
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    
    with open(log_path, 'a') as log_file:
        script_file = str(pathlib.Path(__file__).resolve())
        # Use -u for unbuffered output to ensure logs are written immediately
        cmd = [sys.executable, "-u", script_file] + args_list
        
        kwargs = {}
        if sys.platform == 'win32':
            kwargs['creationflags'] = 0x00000008 # DETACHED_PROCESS
        else:
            kwargs['start_new_session'] = True
            
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            **kwargs
        )
        return proc.pid

class SessionManager:
    def __init__(self, db_path: str = user_db_path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            # Enable Write-Ahead Logging for concurrency
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    interaction_id TEXT,
                    prompt TEXT,
                    status TEXT,
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP,
                    result TEXT,
                    files JSON
                )
            """)
            # Migration: Add PID column if missing (v0.8.1)
            cursor = conn.execute("PRAGMA table_info(sessions)")
            columns = [col[1] for col in cursor.fetchall()]
            if "pid" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN pid INTEGER")
            
            # Migration: Add Recursive Research columns (v0.9.0)
            if "parent_id" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN parent_id INTEGER")
            if "depth" not in columns:
                conn.execute("ALTER TABLE sessions ADD COLUMN depth INTEGER DEFAULT 1")

            # Performance: Add indexes for frequently queried columns (v0.13.2)
            # These prevent full table scans on common lookups like `list`, `show`, and `tree`.
            conn.execute("CREATE INDEX IF NOT EXISTS idx_interaction_id ON sessions (interaction_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_parent_id ON sessions (parent_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON sessions (status);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_updated_at ON sessions (updated_at);")

            conn.commit()

    def create_session(self, interaction_id: str, prompt: str, files: list[str] | None = None, pid: int | None = None, parent_id: int | None = None, depth: int = 1) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO sessions (interaction_id, prompt, status, created_at, updated_at, files, pid, parent_id, depth) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (interaction_id, prompt, "running", datetime.now().isoformat(), datetime.now().isoformat(), json.dumps(files or []), pid, parent_id, depth)
            )
            conn.commit()
            print(f"[DB] Session saved (ID: {cursor.lastrowid})")
            return cursor.lastrowid

    def update_session_pid(self, session_id: int, pid: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE sessions SET pid = ? WHERE id = ?", (pid, session_id))
            conn.commit()

    def update_session_interaction_id(self, session_id: int, interaction_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE sessions SET interaction_id = ?, status = 'running', updated_at = ? WHERE id = ?",
                (interaction_id, datetime.now().isoformat(), session_id)
            )
            conn.commit()

    def update_session(self, interaction_id: str, status: str, result: str | None = None):
        with sqlite3.connect(self.db_path) as conn:
            query = "UPDATE sessions SET status = ?, updated_at = ?"
            params = [status, datetime.now().isoformat()]
            if result:
                query += ", result = ?"
                params.append(result)
            query += " WHERE interaction_id = ?"
            params.append(interaction_id)
            
            conn.execute(query, tuple(params))
            conn.commit()

    def append_to_result(self, interaction_id: str, new_content: str):
        with sqlite3.connect(self.db_path) as conn:
            # Get current result
            row = conn.execute("SELECT result FROM sessions WHERE interaction_id = ?", (interaction_id,)).fetchone()
            if row:
                current_result = row[0] or ""
                updated_result = f"{current_result}\n\n{new_content}"
                conn.execute(
                    "UPDATE sessions SET result = ?, updated_at = ? WHERE interaction_id = ?",
                    (updated_result, datetime.now().isoformat(), interaction_id)
                )
                conn.commit()

    def get_children(self, session_id: int):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM sessions WHERE parent_id = ? ORDER BY id ASC", (session_id,)).fetchall()

    def list_sessions(self, limit: int = 10):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            sessions = conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
            
            # Check for dead processes
            result = []
            for s in sessions:
                s_dict = dict(s)
                if s['status'] == 'running':
                    is_dead = False
                    
                    # 1. Check own PID
                    if s['pid']:
                        try:
                            os.kill(s['pid'], 0)
                        except OSError:
                            is_dead = True
                    
                    # 2. Check Parent Status/PID (if child has no own PID)
                    elif s['parent_id']:
                        # Recursive check up the chain? Or just direct parent?
                        # Direct parent is usually the process owner for our architecture.
                        parent = conn.execute("SELECT pid, status FROM sessions WHERE id = ?", (s['parent_id'],)).fetchone()
                        if parent:
                            # If parent is finished, child should be finished.
                            if parent['status'] in ['completed', 'crashed', 'failed', 'cancelled']:
                                is_dead = True
                            # If parent is running but dead PID
                            elif parent['pid']:
                                try:
                                    os.kill(parent['pid'], 0)
                                except OSError:
                                    is_dead = True
                    
                    if is_dead:
                        s_dict['status'] = 'crashed'
                        conn.execute("UPDATE sessions SET status = 'crashed' WHERE id = ?", (s['id'],))
                        conn.commit()
                        
                result.append(s_dict)
            return result

    def get_session(self, session_id_or_interaction_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            # Try as ID first
            if str(session_id_or_interaction_id).isdigit():
                return conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id_or_interaction_id,)).fetchone()
            # Try as interaction_id
            return conn.execute("SELECT * FROM sessions WHERE interaction_id = ?", (session_id_or_interaction_id,)).fetchone()

    def delete_session(self, session_id_or_interaction_id: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            if str(session_id_or_interaction_id).isdigit():
                cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id_or_interaction_id,))
            else:
                cursor = conn.execute("DELETE FROM sessions WHERE interaction_id = ?", (session_id_or_interaction_id,))
            conn.commit()
            return cursor.rowcount > 0

class DeepResearchConfig(BaseModel):
    api_key: str = Field(default_factory=lambda: os.getenv("GEMINI_API_KEY"), validate_default=True)
    agent_name: str = "deep-research-pro-preview-12-2025"
    followup_model: str = "gemini-3-pro-preview"
    recursion_timeout: int = 600 # 10 minutes per child task

    @field_validator('api_key', mode='before')
    @classmethod
    def check_api_key(cls, v: str) -> str:
        if not v:
            raise ValueError("GEMINI_API_KEY not found. Please set it in .env or ~/.config/deepresearch/.env")
        return v

class DataExporter:
    @staticmethod
    def extract_code_block(text: str, lang: str = "") -> str:
        """Extracts content from a markdown code block."""
        # Regex to find ```lang ... ``` blocks
        pattern = rf"```{lang}\n(.*?)\n```"
        match = re.search(pattern, text, re.DOTALL)
        # Fallback: look for generic blocks if specific lang fails
        if not match and lang:
             pattern = r"```\n(.*?)\n```"
             match = re.search(pattern, text, re.DOTALL)
        return match.group(1) if match else text

    @staticmethod
    def save_json(content: str, filepath: str):
        try:
            # Try to extract JSON from code blocks first
            clean_content = DataExporter.extract_code_block(content, "json")
            # Attempt to parse to ensure validity
            data = json.loads(clean_content)
            
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=2)
            print(f"[INFO] JSON report saved to {filepath}")
        except json.JSONDecodeError as e:
            print(f"[ERROR] Failed to parse JSON output: {e}")
            # Save raw content as fallback
            with open(filepath + ".raw", 'w') as f:
                f.write(content)
            print(f"[WARN] Raw content saved to {filepath}.raw")

    @staticmethod
    def save_csv(content: str, filepath: str):
        try:
            clean_content = DataExporter.extract_code_block(content, "csv")
            with open(filepath, 'w') as f:
                f.write(clean_content)
            print(f"[INFO] CSV report saved to {filepath}")
        except Exception as e:
            print(f"[ERROR] Failed to save CSV: {e}")

    @staticmethod
    def export(content: str, filepath: str):
        if filepath.lower().endswith('.json'):
            DataExporter.save_json(content, filepath)
        elif filepath.lower().endswith('.csv'):
            DataExporter.save_csv(content, filepath)
        else:
            # Default text/markdown save
            with open(filepath, 'w') as f:
                f.write(content)
            print(f"[INFO] Report saved to {filepath}")

class ResearchRequest(BaseModel):
    prompt: str
    stores: list[str] | None = None
    stream: bool = False
    output_format: str | None = None
    upload_paths: list[str] | None = None
    output_file: str | None = None
    adopt_session_id: int | None = None
    depth: int = 1
    breadth: int = 3 # Max child tasks per node

    @property
    def final_prompt(self) -> str:
        base = self.prompt
        if self.output_format:
            base += f"\n\nFormat the output as follows: {self.output_format}"
        
        # Auto-append structural instructions based on filename extension
        if self.output_file:
            if self.output_file.lower().endswith('.json'):
                base += "\n\nIMPORTANT: Output the final report as valid JSON inside a ```json code block."
            elif self.output_file.lower().endswith('.csv'):
                base += "\n\nIMPORTANT: Output the final report as valid CSV inside a ```csv code block."
        
        return base

    @property
    def tools_config(self) -> list[dict] | None:
        if self.stores:
            return [
                {
                    "type": "file_search",
                    "file_search_store_names": self.stores
                }
            ]
        return None

class FollowUpRequest(BaseModel):
    interaction_id: str
    prompt: str

class FileManager:
    def __init__(self, client):
        self.client = client
        self.created_stores = []
        self.uploaded_files = []

    def create_store_from_paths(self, paths: list[str]) -> str:
        console.print(f"[bold cyan][INFO][/] Uploading {len(paths)} items to a new File Search Store...")
        store = self.client.file_search_stores.create()
        self.created_stores.append(store.name)
        console.print(f"[bold cyan][INFO][/] Created temporary store: {store.name}")

        for path in paths:
            if os.path.isdir(path):
                for f in os.listdir(path):
                    full_path = os.path.join(path, f)
                    if os.path.isfile(full_path):
                        self._upload_file(full_path, store.name)
            elif os.path.isfile(path):
                self._upload_file(path, store.name)
            else:
                console.print(f"[bold yellow][WARN][/] Skipped invalid path: {path}")

        console.print("[bold cyan][INFO][/] Waiting 5s for file ingestion...")
        time.sleep(5)
        return store.name

    def _upload_file(self, path: str, store_name: str):
        console.print(f"[bold cyan][INFO][/] Uploading: {path}")
        try:
            # Determine MIME type for source files that might fail auto-detection
            mime_type = None
            if path.endswith(('.py', '.toml', '.md', '.json', '.lock', '.yml', '.yaml', '.txt')):
                mime_type = 'text/plain'

            if hasattr(self.client.file_search_stores, 'upload_to_file_search_store'):
                 # Helper doesn't seem to expose config/mime_type easily in args?
                 # Let's check diagnostics... It had 'config'.
                 # config={'mime_type': ...}
                 
                 kwargs = {'file_search_store_name': store_name, 'file': path}
                 if mime_type:
                     # Attempt to pass config if supported
                     # Based on previous inspection: (*, file_search_store_name, file, config=...)
                     kwargs['config'] = {'mime_type': mime_type}

                 self.client.file_search_stores.upload_to_file_search_store(**kwargs)
            else:
                 # Fallback path
                 upload_config = None
                 if mime_type:
                     upload_config = {'mime_type': mime_type}
                     
                 file_obj = self.client.files.upload(path=path, config=upload_config)
                 self.uploaded_files.append(file_obj.name)
                 
                 # Wait for processing with 5-minute timeout
                 start_time = time.time()
                 while file_obj.state.name == "PROCESSING":
                     if time.time() - start_time > 300: # 300 seconds = 5 minutes
                         raise TimeoutError(f"File processing timed out after 5 minutes: {path}")
                     time.sleep(2)
                     file_obj = self.client.files.get(name=file_obj.name)
        except Exception as e:
            console.print(f"[bold red][ERROR][/] Failed to upload {path}: {e}")
            raise

    def cleanup(self):
        console.print("\n[bold cyan][INFO][/] Cleaning up temporary resources...")
        for store_name in self.created_stores:
             try:
                 # 1. Empty the store first
                 if hasattr(self.client.file_search_stores, 'documents'):
                     try:
                         # List documents in the store
                         pager = self.client.file_search_stores.documents.list(parent=store_name)
                         for doc in pager:
                             try:
                                 console.print(f"[bold cyan][INFO][/] Deleting document: {doc.name}")
                                 # Force delete to remove chunks/non-empty docs
                                 self.client.file_search_stores.documents.delete(
                                     name=doc.name,
                                     config={'force': True}
                                 )
                             except Exception as e:
                                 console.print(f"[bold yellow][WARN][/] Failed to delete document {doc.name}: {e}")
                     except Exception as e:
                         # If listing fails, we might just try deleting the store directly
                         console.print(f"[bold yellow][WARN][/] Failed to list documents in {store_name}: {e}")

                 # 2. Delete the store
                 self.client.file_search_stores.delete(name=store_name)
                 console.print(f"[bold cyan][INFO][/] Deleted store: {store_name}")
             except Exception as e:
                 if "non-empty" in str(e):
                     console.print(f"[bold yellow][WARN][/] Could not delete store {store_name} (contains files). It will persist.")
                 else:
                     console.print(f"[bold yellow][WARN][/] Failed to delete store {store_name}: {e}")

        # Note: We don't need to delete 'uploaded_files' via client.files.delete() 
        # if they were uploaded via upload_to_file_search_store() as they are managed by the store?
        # Actually, 'upload_to_file_search_store' creates a Document. 
        # Does it create a File resource too? 
        # Usually yes. But if we delete the Document, does it delete the File?
        # Let's keep the file deletion logic just in case we used the fallback upload method.
        for file_name in self.uploaded_files:
            try:
                self.client.files.delete(name=file_name)
                console.print(f"[bold cyan][INFO][/] Deleted file resource: {file_name}")
            except Exception:
                pass

class DeepResearchAgent:
    def __init__(self, config: DeepResearchConfig | None = None, logger=None, quiet: bool = False):
        self.config = config or DeepResearchConfig()
        self.client = genai.Client(api_key=self.config.api_key)
        self.file_manager = FileManager(self.client)
        self.session_manager = SessionManager()
        self.logger = logger
        self.quiet = quiet

    def _log(self, message: str, end: str = "\n", **kwargs):
        """Internal logging helper that respects the custom logger."""
        if self.quiet:
            return

        if self.logger:
            self.logger(message)
        else:
            # Rich styling
            msg = message
            if "[INFO]" in message:
                msg = message.replace("[INFO]", "[bold cyan][INFO][/]")
            elif "[THOUGHT]" in message:
                msg = message.replace("[THOUGHT]", "[bold magenta][THOUGHT][/]")
            elif "[ERROR]" in message:
                msg = message.replace("[ERROR]", "[bold red][ERROR][/]")
            elif "[WARN]" in message:
                msg = message.replace("[WARN]", "[bold yellow][WARN][/]")
            elif "[DB]" in message:
                msg = message.replace("[DB]", "[bold green][DB][/]")
            
            # Rich print doesn't support 'flush', so we pop it
            kwargs.pop('flush', None)
            
            # Safety: If message is huge (e.g. > 10KB report), Rich Markdown parsing might crash
            # in headless/detached mode. Fallback to raw print.
            if len(msg) > 10000:
                print(msg, end=end, flush=True)
            else:
                console.print(msg, end=end, highlight=False, **kwargs)

    def _process_stream(self, event_stream, interaction_id_ref: list, last_event_id_ref: list, is_complete_ref: list, request_prompt: str | None = None, upload_paths: list | None = None, adopt_session_id: int | None = None):
        for event in event_stream:
            if event.event_type == "interaction.start":
                interaction_id_ref[0] = event.interaction.id
                self._log(f"\n[INFO] Interaction started: {event.interaction.id}")
                if adopt_session_id:
                    self.session_manager.update_session_interaction_id(adopt_session_id, event.interaction.id)
                elif request_prompt:
                    self.session_manager.create_session(event.interaction.id, request_prompt, upload_paths)

            if event.event_id:
                last_event_id_ref[0] = event.event_id
            if event.event_type == "content.delta":
                if event.delta.type == "text":
                    self._log(event.delta.text, end="")
                elif event.delta.type == "thought_summary":
                    self._log(f"\n[THOUGHT] {event.delta.content.text}", flush=True)
            if event.event_type in ['interaction.complete', 'error']:
                is_complete_ref[0] = True

    def start_research_stream(self, request: ResearchRequest, auto_update_status: bool = True):
        agent_config = {
            "type": "deep-research",
            "thinking_summaries": "auto"
        }
        
        # Handle Auto-Upload
        if request.upload_paths:
            try:
                store_name = self.file_manager.create_store_from_paths(request.upload_paths)
                if request.stores is None:
                    request.stores = []
                request.stores.append(store_name)
                
                # FORCE PRIORITY
                request.prompt = (
                    f"{request.prompt}\n\n"
                    "IMPORTANT: You have access to a File Search Store containing uploaded documents. "
                    "You MUST search these files FIRST and prioritize their content over public web results. "
                    "If the answer is found in the uploaded files, cite them explicitly."
                )
            except Exception as e:
                self._log(f"[ERROR] File upload failed: {e}")
                self.file_manager.cleanup()
                return None

        last_event_id = [None]
        interaction_id = [None]
        is_complete = [False]

        if request.stores:
            self._log(f"[INFO] Using File Search Stores: {request.stores}")

        try:
            self._log("[INFO] Starting Research Stream...")
            if not hasattr(self.client, 'interactions'):
                import google.genai
                raise RuntimeError(f"google-genai version {google.genai.__version__} too old.")

            initial_stream = self.client.interactions.create(
                input=request.final_prompt,
                agent=self.config.agent_name,
                background=True,
                stream=True,
                tools=request.tools_config,
                agent_config=agent_config
            )
            # Pass prompt for DB saving
            self._process_stream(initial_stream, interaction_id, last_event_id, is_complete, request.prompt, request.upload_paths, request.adopt_session_id)
            
            # Reconnection Loop
            while not is_complete[0] and interaction_id[0]:
                self._log(f"\n[INFO] Connection lost. Resuming from {last_event_id[0]}...")
                time.sleep(2)
                try:
                    resume_stream = self.client.interactions.get(
                        id=interaction_id[0], stream=True, last_event_id=last_event_id[0]
                    )
                    self._process_stream(resume_stream, interaction_id, last_event_id, is_complete, adopt_session_id=request.adopt_session_id)
                except Exception as e:
                    self._log(f"[ERROR] Reconnection failed: {e}")
            
            if is_complete[0]:
                 self._log("\n[INFO] Research Complete.")
                 
                 # Retrieve final text
                 if interaction_id[0]:
                     try:
                         final_interaction = self.client.interactions.get(id=interaction_id[0])
                         if final_interaction.outputs:
                             final_text = final_interaction.outputs[-1].text
                             
                             if self.quiet:
                                 # In quiet mode, print ONLY the result for piping
                                 print(final_text)
                             
                             if auto_update_status:
                                 self.session_manager.update_session(interaction_id[0], "completed", final_text)
                             else:
                                 self.session_manager.update_session(interaction_id[0], "running", final_text)
                                 
                             if request.output_file:
                                 DataExporter.export(final_text, request.output_file)
                     except Exception as e:
                         self._log(f"[WARN] Failed to retrieve/export result: {e}")

        except KeyboardInterrupt:
            self._log("\n[WARN] Research interrupted by user.")
            if interaction_id[0]:
                self.session_manager.update_session(interaction_id[0], "cancelled")
        except Exception as e:
            self._log(f"\n[ERROR] Research failed: {e}")
            if interaction_id[0]:
                self.session_manager.update_session(interaction_id[0], "failed", result=f"Exception: {e}")
        finally:
            if request.upload_paths:
                self.file_manager.cleanup()
        
        return interaction_id[0]

    def start_research_poll(self, request: ResearchRequest, auto_update_status: bool = True):
        if request.upload_paths:
             try:
                store_name = self.file_manager.create_store_from_paths(request.upload_paths)
                if request.stores is None:
                    request.stores = []
                request.stores.append(store_name)
             except Exception as e:
                self._log(f"[ERROR] Upload failed: {e}")
                self.file_manager.cleanup()
                return

        if request.stores:
             self._log(f"[INFO] Using Stores: {request.stores}")

        self._log("[INFO] Starting Research (Polling)...")
        try:
            interaction = self.client.interactions.create(
                input=request.final_prompt,
                agent=self.config.agent_name,
                background=True,
                tools=request.tools_config
            )
            self._log(f"[INFO] Started: {interaction.id}")
            
            if hasattr(request, 'adopt_session_id') and request.adopt_session_id:
                self.session_manager.update_session_interaction_id(request.adopt_session_id, interaction.id)
            else:
                self.session_manager.create_session(interaction.id, request.prompt, request.upload_paths)

            while True:
                interaction = self.client.interactions.get(interaction.id)
                if interaction.status == "completed":
                    self._log("\n" + "="*40 + " REPORT " + "="*40)
                    final_text = interaction.outputs[-1].text
                    
                    # Log snippet only to prevent crash on massive output
                    if len(final_text) > 2000:
                        self._log(final_text[:2000] + "\n\n... [Report Truncated in Logs. Full content in DB] ...")
                    else:
                        self._log(final_text)
                    
                    if self.quiet:
                        print(final_text)

                    if auto_update_status:
                        self.session_manager.update_session(interaction.id, "completed", final_text)
                    else:
                        # Recursive Node: Save intermediate result but keep running
                        self.session_manager.update_session(interaction.id, "running", final_text)
                    
                    if request.output_file:
                        DataExporter.export(final_text, request.output_file)
                    break
                elif interaction.status == "failed":
                    error_msg = f"API Error: {interaction.error}"
                    self._log(f"[ERROR] Failed: {interaction.error}")
                    self.session_manager.update_session(interaction.id, "failed", result=error_msg)
                    break
                if not self.logger:
                    sys.stdout.write(".")
                    sys.stdout.flush()
                time.sleep(10)
        except KeyboardInterrupt:
            self._log("\n[WARN] Polling interrupted by user.")
            if 'interaction' in locals() and hasattr(interaction, 'id'):
                 self.session_manager.update_session(interaction.id, "cancelled")
        except Exception as e:
            self._log(f"[ERROR] Unexpected error: {e}")
            if 'interaction' in locals() and hasattr(interaction, 'id'):
                 self.session_manager.update_session(interaction.id, "failed", result=f"Exception: {e}")
        finally:
            if request.upload_paths:
                self.file_manager.cleanup()
        return interaction.id

    def follow_up(self, request: FollowUpRequest):
        self._log(f"[INFO] Sending follow-up to interaction: {request.interaction_id}")
        try:
            interaction = self.client.interactions.create(
                input=request.prompt,
                model=self.config.followup_model, 
                previous_interaction_id=request.interaction_id
            )
            if interaction.outputs:
                response_text = interaction.outputs[-1].text
                self._log(response_text)
                
                # Save to DB
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                append_text = f"\n\n---\n### Follow-up ({timestamp})\n\n**Q: {request.prompt}**\n\n{response_text}"
                self.session_manager.append_to_result(request.interaction_id, append_text)
        except Exception as e:
            self._log(f"[ERROR] Follow-up failed: {e}")

    def analyze_gaps(self, original_prompt: str, report_text: str, limit: int = 3) -> list[str]:
        self._log(f"[THOUGHT] Analyzing report for gaps (Limit: {limit})...")
        
        prompt = (
            f"Original Objective: {original_prompt}\n\n"
            f"Report:\n{report_text}\n\n"
            "INSTRUCTIONS:\n"
            "1. Analyze the report against the objective.\n"
            f"2. Identify 1-{limit} critical gaps, unanswered questions, or areas needing deeper verification.\n"
            "3. If the report is comprehensive, return an empty list.\n"
            "4. Output strictly a JSON list of strings, e.g., [\"Question 1\", \"Question 2\"].\n"
            "5. Wrap the JSON in a ```json code block."
        )
        
        try:
            self._log("[DEBUG] Sending gap analysis request...")
            response = self.client.models.generate_content(
                model=self.config.followup_model,
                contents=prompt
            )
            text = response.text
            self._log(f"[DEBUG] Gap analysis response: {text[:100]}...")
            
            # Use DataExporter utility to extract JSON
            json_str = DataExporter.extract_code_block(text, "json")
            if not json_str:
                # If extraction fails, assume no gaps or parsing error
                # We could try regex finding simple list items but JSON is safer
                return []
            return json.loads(json_str)
        except Exception as e:
            self._log(f"[WARN] Failed to analyze gaps: {e}")
            return []

    def synthesize_findings(self, original_prompt: str, main_report: str, sub_reports: list[str]) -> str:
        self._log(f"[THOUGHT] Synthesizing {len(sub_reports)} child reports into final answer...")
        
        combined_subs = "\n\n---\n\n".join([f"Sub-Report {i+1}:\n{r}" for i, r in enumerate(sub_reports)])
        
        prompt = (
            f"Objective: {original_prompt}\n\n"
            f"Initial Research Findings:\n{main_report}\n\n"
            f"Deep Dive Findings (Sub-Reports):\n{combined_subs}\n\n"
            "INSTRUCTIONS:\n"
            "1. Synthesize all information into a single, cohesive, comprehensive report.\n"
            "2. Integrate the Deep Dive findings naturally into the narrative (do not just append them).\n"
            "3. Resolve any conflicts between reports.\n"
            "4. Maintain a professional, 'Deep Research' tone."
        )
        
        try:
            response = self.client.models.generate_content(
                model=self.config.followup_model,
                contents=prompt
            )
            return response.text
        except Exception as e:
            self._log(f"[ERROR] Synthesis failed: {e}")
            return main_report + "\n\n[ERROR: Synthesis failed. Appending raw sub-reports below]\n\n" + combined_subs

    def start_recursive_research(self, request: ResearchRequest):
        """Entry point for recursive research."""
        final_result = self._execute_recursion_level(
            prompt=request.prompt,
            current_depth=1,
            max_depth=request.depth,
            breadth=request.breadth,
            original_request=request
        )
        
        if final_result:
            self._log("[INFO] Recursive Research Complete.")
            sys.stdout.flush()

        if request.output_file and final_result:
             DataExporter.export(final_result, request.output_file)

    def _execute_recursion_level(self, prompt: str, current_depth: int, max_depth: int, breadth: int, original_request: ResearchRequest, parent_id: int | None = None) -> str | None:
        indent = "  " * (current_depth - 1)
        if current_depth > 1:
            self._log(f"{indent}[INFO] Recursive Step Depth {current_depth}/{max_depth}: {prompt}")
        else:
            self._log(f"[INFO] Starting Recursive Research (Depth {max_depth}, Breadth {breadth})")

        # 1. Prepare Request
        node_req = ResearchRequest(
            prompt=prompt,
            upload_paths=original_request.upload_paths,
            stores=original_request.stores,
            stream=(current_depth == 1), # Stream Root only
            depth=current_depth
        )

        # 2. Execute Research (Poll or Stream)
        # Only mark 'completed' automatically if this is a LEAF node (no further recursion)
        is_leaf = (current_depth >= max_depth)
        
        if current_depth == 1:
             # Root: Stream to show thoughts in logs
             interaction_id = self.start_research_stream(node_req, auto_update_status=is_leaf)
        else:
             # Child: Poll to avoid log interleaving
             child_sid = self.session_manager.create_session(
                 "pending_recursion", prompt, original_request.upload_paths, parent_id=parent_id, depth=current_depth
             )
             node_req.adopt_session_id = child_sid
             interaction_id = self.start_research_poll(node_req, auto_update_status=is_leaf)

        # 3. Fetch Result
        session = self.session_manager.get_session(interaction_id)
        
        # Robustness: If auto_update_status=False, status might be 'running' but result is ready.
        # We proceed if we have a result.
        if not session:
            self._log(f"{indent}[ERROR] Research session not found.")
            return None
            
        # session is sqlite3.Row, doesn't support .get(). Use index access or dict conversion.
        status = session['status']
        result = session['result']
        
        if status != 'completed' and not result:
            self._log(f"{indent}[ERROR] Research failed or incomplete. Status: {status}")
            return None
        
        report = result
        current_id = session['id']
        self._log(f"{indent}[INFO] Phase 1 complete. Report length: {len(report)} chars.")

        # 4. Check Termination
        if current_depth >= max_depth:
            return report

        # 5. Analyze Gaps
        self._log(f"{indent}[INFO] Analyzing gaps...")
        questions = self.analyze_gaps(prompt, report, limit=breadth)
        self._log(f"{indent}[INFO] Gaps found: {len(questions)}")
        
        if not questions:
            # Recursion ends here (Leaf by logic)
            self.session_manager.update_session(interaction_id, "completed", result=report)
            return report

        self._log(f"{indent}[INFO] Spawning {len(questions)} sub-tasks...")

        # 6. Recurse in Parallel
        sub_reports = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=breadth) as executor:
            futures = []
            for q in questions:
                futures.append(executor.submit(
                    self._run_recursive_child_safe, 
                    q, current_depth + 1, max_depth, breadth, original_request, current_id
                ))
            
            # Enforce timeout
            done, not_done = concurrent.futures.wait(futures, timeout=self.config.recursion_timeout)
            
            for f in done:
                try:
                    res = f.result()
                    if res:
                        sub_reports.append(res)
                except Exception as e:
                    self._log(f"{indent}[WARN] Child failed: {e}")
            
            if not_done:
                self._log(f"{indent}[ERROR] {len(not_done)} child tasks timed out.")

        if not sub_reports:
            return report

        # 7. Synthesis
        final_report = self.synthesize_findings(prompt, report, sub_reports)
        
        # Update Session with synthesis
        self.session_manager.update_session(interaction_id, "completed", result=final_report)
        
        return final_report

    def _run_recursive_child_safe(self, q, d, max_d, b, req, pid):
        # Helper to instantiate agent and run recursion in thread
        agent = DeepResearchAgent(config=self.config)
        return agent._execute_recursion_level(q, d, max_d, b, req, pid)

def main():
    desc = """
Gemini Deep Research Agent CLI
==============================
A powerful tool to conduct autonomous, multi-step research using Gemini 3 Pro.
Support web search, local file ingestion, streaming thoughts, and follow-ups.
    """
    
    epilog = """
Examples:
---------
1. Basic Web Research (Streaming):
   %(prog)s research "History of the internet" --stream

2. Research with Local Files (Smart Context):
   %(prog)s research "Summarize this contract" --upload ./contract.pdf --stream

3. Formatted Output & Export:
   %(prog)s research "Compare GPU prices" --format "Markdown table" --output prices.md
   %(prog)s research "List top 5 cloud providers" --output market_data.json

4. Headless Research (Fire & Forget):
   %(prog)s start "Detailed analysis of quantum computing"
   # ... process detaches ...
   %(prog)s list
   %(prog)s show 1

5. Follow-up Question:
   %(prog)s followup 1 "Can you explain the error correction?"

6. Manage History:
   %(prog)s list
   %(prog)s show 1

Configuration:
--------------
Set GEMINI_API_KEY in a local .env file or at ~/.config/deepresearch/.env
    """

    parser = argparse.ArgumentParser(
        description=desc,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {get_version()}")

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Command: research
    parser_research = subparsers.add_parser("research", help="Start a new research task")
    parser_research.add_argument("prompt", help="The research prompt or question")
    parser_research.add_argument("-q", "--quiet", action="store_true", help="Suppress logs, output only final report (Good for piping)")
    parser_research.add_argument("--stream", action="store_true", help="Stream the agent's thought process (Recommended)")
    parser_research.add_argument("--stores", nargs="+", help="Existing Cloud File Search Store names (advanced)")
    parser_research.add_argument("--upload", nargs="+", help="Local file/folder paths to upload, analyze, and auto-delete")
    parser_research.add_argument("--format", help="Specific output instructions (e.g., 'Technical Report', 'CSV')")
    parser_research.add_argument("--output", help="Save report to file (e.g., report.md, data.json)")
    parser_research.add_argument("--depth", type=int, default=1, help="Recursive research depth (default: 1)")
    parser_research.add_argument("--breadth", type=int, default=3, help="Max child tasks per recursion level (default: 3)")
    parser_research.add_argument("--adopt-session", type=int, help=argparse.SUPPRESS)

    # Command: start (Headless)
    parser_start = subparsers.add_parser("start", help="Start a research task in the background (Headless)")
    parser_start.add_argument("prompt", help="The research prompt or question")
    parser_start.add_argument("--upload", nargs="+", help="Local file/folder paths to upload")
    parser_start.add_argument("--format", help="Specific output instructions")
    parser_start.add_argument("--output", help="Save report to file")
    parser_start.add_argument("--depth", type=int, default=1, help="Recursive research depth (default: 1)")
    parser_start.add_argument("--breadth", type=int, default=3, help="Max child tasks per recursion level (default: 3)")

    # Command: followup
    parser_followup = subparsers.add_parser("followup", help="Ask a follow-up question to a previous session")
    parser_followup.add_argument("id", help="The Interaction ID from a previous research task")
    parser_followup.add_argument("prompt", help="The follow-up question")

    # Command: list
    parser_list = subparsers.add_parser("list", help="List recent research sessions")
    parser_list.add_argument("--limit", type=int, default=10, help="Number of sessions to show")

    # Command: show
    parser_show = subparsers.add_parser("show", help="Show details of a previous session")
    parser_show.add_argument("id", help="Session ID (integer) or Interaction ID")
    parser_show.add_argument("--save", help="Save the colorful report to HTML or Text file")
    parser_show.add_argument("--recursive", action="store_true", help="Include all child session reports in output")

    # Command: delete
    parser_delete = subparsers.add_parser("delete", help="Delete a session from history")
    parser_delete.add_argument("id", help="Session ID (integer) or Interaction ID")

    # Command: cleanup
    parser_cleanup = subparsers.add_parser("cleanup", help="Delete stale cloud resources (GC)")
    parser_cleanup.add_argument("--force", action="store_true", help="Delete without confirmation")

    # Command: tree
    parser_tree = subparsers.add_parser("tree", help="Visualize session hierarchy")
    parser_tree.add_argument("id", nargs="?", help="Root Session ID (optional)")

    # Command: auth
    parser_auth = subparsers.add_parser("auth", help="Manage authentication")
    parser_auth.add_argument("action", choices=["login", "logout"], help="Action to perform")

    # Command: estimate
    parser_estimate = subparsers.add_parser("estimate", help="Estimate cost of a research task")
    parser_estimate.add_argument("prompt", help="The research prompt or question")
    parser_estimate.add_argument("--depth", type=int, default=1, help="Recursive depth")
    parser_estimate.add_argument("--breadth", type=int, default=3, help="Recursive breadth")
    parser_estimate.add_argument("--upload", nargs="+", help="Files to upload (adds to context cost)")

    # Default Command Logic
    # If the first argument is not a known subcommand, assume 'research'.
    known_commands = {
        "research", "start", "followup", "list", "show", 
        "delete", "cleanup", "tree", "auth", "estimate",
        "-h", "--help", "-v", "--version"
    }
    
    if len(sys.argv) > 1 and sys.argv[1] not in known_commands:
        sys.argv.insert(1, "research")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    try:
        if args.command == "start":
            # 1. Create Placeholder Session
            mgr = SessionManager()
            sid = mgr.create_session("pending_start", args.prompt, args.upload)
            
            # 2. Construct Child Arguments
            child_args = ["research", args.prompt, "--adopt-session", str(sid)]
            if args.upload:
                 child_args += ["--upload"] + args.upload
            if args.format:
                 child_args += ["--format", args.format]
            if args.output:
                 child_args += ["--output", args.output]
            
            # Pass recursion params
            child_args += ["--depth", str(args.depth)]
            child_args += ["--breadth", str(args.breadth)]
            
            # 3. Detach
            log_file = os.path.join(xdg_config_home, "deepresearch", "logs", f"session_{sid}.log")
            pid = detach_process(child_args, log_file)
            mgr.update_session_pid(sid, pid)
            
            print(f"[INFO] Research started in background! (Session ID: {sid}, PID: {pid})")
            print(f"[INFO] Logs: {log_file}")
            print("[INFO] Check status with: deep-research list")

        elif args.command == "research":
            request = ResearchRequest(
                prompt=args.prompt,
                stores=args.stores,
                stream=args.stream,
                output_format=args.format,
                upload_paths=args.upload,
                output_file=args.output,
                adopt_session_id=args.adopt_session,
                depth=args.depth,
                breadth=args.breadth
            )
            # Default to stream if not quiet, unless explicitly set
            if not args.quiet and not args.stream and request.depth == 1:
                 # Auto-stream for interactive use? No, keep existing behavior (default False?)
                 # Actually, argparse default is False.
                 # Let's keep it explicit.
                 pass

            agent = DeepResearchAgent(quiet=args.quiet)
            
            if request.depth > 1:
                if request.stream and not args.quiet:
                    print("[INFO] Recursive research does not support streaming to stdout. Switching to polling mode.")
                agent.start_recursive_research(request)
            elif request.stream:
                agent.start_research_stream(request)
            else:
                agent.start_research_poll(request)

        elif args.command == "followup":
            interaction_id = args.id
            
            # Smart Lookup: If ID is numeric, look it up in DB
            if args.id.isdigit():
                mgr = SessionManager()
                session = mgr.get_session(args.id)
                if session and session['interaction_id']:
                    print(f"[INFO] Resuming Session #{args.id} (Interaction: {session['interaction_id']})")
                    interaction_id = session['interaction_id']
                else:
                    print(f"[ERROR] Session #{args.id} not found or invalid.")
                    return

            request = FollowUpRequest(
                interaction_id=interaction_id,
                prompt=args.prompt
            )
            agent = DeepResearchAgent()
            agent.follow_up(request)

        elif args.command == "list":
            mgr = SessionManager()
            sessions = mgr.list_sessions(args.limit)
            
            table = Table(title="Recent Research Sessions", box=None)
            table.add_column("ID", style="cyan", no_wrap=True)
            table.add_column("Status")
            table.add_column("Date", style="dim")
            table.add_column("Prompt", style="bold")

            for s in sessions:
                # Replace newlines for cleaner table but keep full length
                prompt = s['prompt'].replace('\n', ' ')
                
                status_style = "green" if s['status'] == "completed" else "yellow" if s['status'] == "running" else "red"
                status_text = f"[{status_style}]{s['status']}[/{status_style}]"
                
                table.add_row(str(s['id']), status_text, s['created_at'][:19], prompt)
            
            console.print(table)

        elif args.command == "show":
            mgr = SessionManager()
            
            def get_full_recursive_report(root_id, level=1):
                session = mgr.get_session(root_id)
                if not session:
                    return ""
                
                # Header
                indent_hash = "#" * min(level, 6)
                title = session['prompt'].replace('\n', ' ')
                
                report = f"{indent_hash} Session #{session['id']} (Depth {session['depth']})\n"
                report += f"**Objective:** {title}\n"
                report += f"**Status:** {session['status']}\n\n"
                
                if session['result']:
                    report += session['result']
                else:
                    report += "*(No content)*"
                
                report += "\n\n---\n\n"
                
                # Children
                children = mgr.get_children(root_id)
                for child in children:
                    report += get_full_recursive_report(child['id'], level + 1)
                
                return report

            if args.recursive:
                full_content = get_full_recursive_report(args.id)
                if not full_content:
                    console.print(f"[bold red]Session {args.id} not found.[/]")
                else:
                    console.print(Markdown(full_content))
                    if args.save:
                        if args.save.lower().endswith('.html'):
                            # For HTML, we render the Markdown to console (record=True) then save
                            # But wait, printing to console might be huge.
                            # We should use a separate console for saving.
                            save_console = Console(record=True)
                            save_console.print(Markdown(full_content))
                            save_console.save_html(args.save, theme=MONOKAI)
                        else:
                            with open(args.save, 'w') as f:
                                f.write(full_content)
                        console.print(f"[bold green]Recursive report saved to {args.save}[/]")
                return

            session = mgr.get_session(args.id)
            
            # Use recording console if saving
            show_console = Console(record=True) if args.save else console

            if not session:
                show_console.print(f"[bold red][ERROR] Session '{args.id}' not found.[/]")
            else:
                show_console.print(Panel(
                    f"[bold]Interaction ID:[/bold] {session['interaction_id']}\n"
                    f"[bold]Date:[/bold] {session['created_at']}\n"
                    f"[bold]Status:[/bold] {session['status']}\n"
                    f"[bold]Files:[/bold] {session['files']}",
                    title=f"Session #{session['id']}",
                    subtitle="Metadata"
                ))
                
                show_console.rule("[bold cyan]Prompt[/]")
                show_console.print(f"[bold]{session['prompt']}[/]\n")
                
                show_console.rule("[bold green]Result[/]")
                if session['result']:
                    show_console.print(Markdown(session['result']))
                else:
                    show_console.print("[italic dim](No result stored)[/]")
            
            if args.save:
                if args.save.lower().endswith('.html'):
                    # Use Monokai (Dark) theme for HTML export
                    show_console.save_html(args.save, theme=MONOKAI)
                else:
                    show_console.save_text(args.save)
                console.print(f"[bold green][INFO][/] Report saved to {args.save}")

        elif args.command == "delete":
            mgr = SessionManager()
            success = mgr.delete_session(args.id)
            if success:
                console.print(f"[bold green][INFO][/] Session '{args.id}' deleted.")
            else:
                console.print(f"[bold red][ERROR][/] Session '{args.id}' not found.")

        elif args.command == "cleanup":
            config = DeepResearchConfig()
            client = genai.Client(api_key=config.api_key)
            
            console.print("[bold cyan][INFO][/] Scanning for File Search Stores...")
            # Note: client.file_search_stores.list() returns an iterator
            try:
                stores = list(client.file_search_stores.list())
            except Exception as e:
                console.print(f"[bold red][ERROR][/] Failed to list stores: {e}")
                return

            if not stores:
                console.print("[bold green]No active stores found. System is clean![/]")
                return

            table = Table(title=f"Found {len(stores)} Active Cloud Stores")
            table.add_column("Name (ID)", style="cyan")
            table.add_column("Create Time", style="dim")
            
            for s in stores:
                # s.name is like 'fileSearchStores/xyz'
                # s.create_time might be available depending on SDK version
                created = getattr(s, 'create_time', 'Unknown')
                table.add_row(s.name, str(created))
            
            console.print(table)
            console.print("[bold yellow]WARNING: This will delete ALL listed stores and their files.[/]")
            
            if not args.force:
                confirm = input(f"Are you sure you want to delete {len(stores)} stores? [y/N] ")
                if confirm.lower() != 'y':
                    console.print("[bold yellow]Aborted.[/]")
                    return

            with console.status("Deleting stores...", spinner="dots"):
                for s in stores:
                    # 1. Empty the store
                    try:
                        if hasattr(client.file_search_stores, 'documents'):
                            docs = list(client.file_search_stores.documents.list(parent=s.name))
                            if docs:
                                console.print(f"  Emptying {len(docs)} documents...")
                            for doc in docs:
                                try:
                                    # Force delete is required if document has content
                                    client.file_search_stores.documents.delete(name=doc.name, config={'force': True})
                                except Exception as e:
                                    console.print(f"  [yellow]Failed to delete doc {doc.name}: {e}[/]")
                    except Exception as e:
                        console.print(f"  [yellow]Failed to list docs: {e}[/]")

                    # 2. Delete the store
                    try:
                        client.file_search_stores.delete(name=s.name)
                        console.print(f"[green]Deleted:[/green] {s.name}")
                    except Exception as e:
                        console.print(f"[bold red]Failed to delete {s.name}:[/] {e}")
            
            console.print("[bold green]Cleanup Complete![/]")

        elif args.command == "tree":
            mgr = SessionManager()
            
            def build_tree(node_id, tree_node):
                children = mgr.get_children(node_id)
                for child in children:
                    status_style = "green" if child['status'] == "completed" else "red" if child['status'] == "crashed" or child['status'] == "failed" else "yellow"
                    # Clean prompt newlines but keep length
                    prompt = child['prompt'].replace('\n', ' ')
                    if len(prompt) > 100:
                        prompt = prompt[:97] + "..."
                    
                    label = f"#{child['id']} [{status_style}]{child['status']}[/] [dim]Depth {child['depth']}[/]\n[italic]{prompt}[/]"
                    branch = tree_node.add(label)
                    build_tree(child['id'], branch)

            if args.id:
                root = mgr.get_session(args.id)
                if not root:
                    console.print(f"[bold red]Session {args.id} not found[/]")
                    return
                root_label = f"[bold cyan]Session #{root['id']}[/] [dim]Depth {root['depth']}[/]"
                t = Tree(root_label)
                build_tree(root['id'], t)
                console.print(t)
            else:
                forest = Tree("[bold]Recent Research Trees[/]")
                # Get recent roots
                with sqlite3.connect(mgr.db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    roots = conn.execute("SELECT * FROM sessions WHERE parent_id IS NULL ORDER BY updated_at DESC LIMIT 10").fetchall()
                
                for r in roots:
                    status_style = "green" if r['status'] == "completed" else "red" if r['status'] == "crashed" or r['status'] == "failed" else "yellow"
                    prompt = r['prompt'].replace('\n', ' ')
                    if len(prompt) > 100:
                        prompt = prompt[:97] + "..."
                    
                    label = f"#{r['id']} [{status_style}]{r['status']}[/]\n[italic]{prompt}[/]"
                    branch = forest.add(label)
                    build_tree(r['id'], branch)
                console.print(forest)

        elif args.command == "auth":
            if args.action == "login":
                console.print(Panel("Enter your Gemini API Key. It will be stored securely in `~/.config/deepresearch/.env`.", title="Authentication"))
                key = Prompt.ask("API Key", password=True)
                if not key.startswith("AIza"):
                    console.print("[yellow]Warning: Key does not start with 'AIza'. It might be invalid.[/]")
                
                # Write to file
                os.makedirs(os.path.dirname(user_config_path), exist_ok=True)
                with open(user_config_path, 'w') as f:
                    f.write(f"GEMINI_API_KEY={key}\n")
                
                console.print(f"[bold green]Success![/] Key saved to {user_config_path}")
            
            elif args.action == "logout":
                if os.path.exists(user_config_path):
                    os.remove(user_config_path)
                    console.print("[green]Logged out. Config file deleted.[/]")
                else:
                    console.print("[yellow]Not logged in.[/]")

        elif args.command == "estimate":
            # Gemini 3 Pro Pricing (Standard Context)
            COST_INPUT_1M = 2.00
            COST_OUTPUT_1M = 12.00
            
            # Assumptions per Agent Node (Deep Research is token heavy)
            AVG_INPUT_TOKENS = 60_000  # Search results + web pages + internal thought trace
            AVG_OUTPUT_TOKENS = 4_000  # The Markdown report
            
            # Calculate File Tokens
            file_tokens = 0
            if args.upload:
                for path in args.upload:
                    try:
                        if os.path.isdir(path):
                            for root, _, files in os.walk(path):
                                for f in files:
                                    size = os.path.getsize(os.path.join(root, f))
                                    file_tokens += size * 0.25 # Approx 1 token = 4 bytes
                        else:
                            size = os.path.getsize(path)
                            file_tokens += size * 0.25
                    except Exception:
                        pass
            
            # Calculate Total Nodes in Tree
            # Depth 1 = 1 node
            # Depth 2 = 1 + breadth
            # Depth 3 = 1 + breadth + breadth^2
            total_nodes = 0
            for d in range(args.depth):
                nodes_at_level = pow(args.breadth, d)
                total_nodes += nodes_at_level
            
            # Total Tokens
            # Input: Each node reads standard context + uploaded files
            total_input = (total_nodes * AVG_INPUT_TOKENS) + (total_nodes * file_tokens)
            total_output = total_nodes * AVG_OUTPUT_TOKENS
            
            cost = (total_input / 1_000_000 * COST_INPUT_1M) + (total_output / 1_000_000 * COST_OUTPUT_1M)
            
            table = Table(title="Cost Estimate (Gemini 3 Pro)")
            table.add_column("Metric", style="cyan")
            table.add_column("Value", style="bold yellow")
            
            table.add_row("Recursion Depth", str(args.depth))
            table.add_row("Breadth (Fan-out)", str(args.breadth))
            table.add_row("Total Agent Nodes", str(total_nodes))
            table.add_row("File Context", f"{file_tokens:,.0f} tokens")
            table.add_row("Est. Input Tokens", f"{total_input:,.0f}")
            table.add_row("Est. Output Tokens", f"{total_output:,.0f}")
            table.add_row("Estimated Cost", f"${cost:.2f}")
            
            console.print(table)
            console.print("[dim]Pricing: $2.00/1M Input, $12.00/1M Output. Actuals may vary based on search grounding.[/]")

        else:
            parser.print_help()

    except ValidationError as e:
        print(f"[ERROR] Input Validation Failed:\n{e}")
    except ValueError as e:
        print(f"[CONFIG ERROR] {e}")
    except Exception as e:
        print(f"[CRITICAL ERROR] {e}")

if __name__ == "__main__":
    main()
                

        