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
from datetime import datetime
from typing import Optional, List
from importlib.metadata import version, PackageNotFoundError
from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel, Field, ValidationError

# Load environment variables
load_dotenv()
xdg_config_home = os.getenv("XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config"))
user_config_path = os.path.join(xdg_config_home, "deepresearch", ".env")
user_db_path = os.path.join(xdg_config_home, "deepresearch", "history.db")
load_dotenv(user_config_path)

# Fallback version if not installed as a package
__version__ = "0.3.0"

def get_version():
    try:
        return version("deepresearch")
    except PackageNotFoundError:
        return __version__

class SessionManager:
    def __init__(self, db_path: str = user_db_path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
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
            conn.commit()

    def create_session(self, interaction_id: str, prompt: str, files: List[str] = None) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "INSERT INTO sessions (interaction_id, prompt, status, created_at, updated_at, files) VALUES (?, ?, ?, ?, ?, ?)",
                (interaction_id, prompt, "running", datetime.now().isoformat(), datetime.now().isoformat(), json.dumps(files or []))
            )
            conn.commit()
            print(f"[DB] Session saved (ID: {cursor.lastrowid})")
            return cursor.lastrowid

    def update_session(self, interaction_id: str, status: str, result: str = None):
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

    def list_sessions(self, limit: int = 10):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()

    def get_session(self, session_id_or_interaction_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            # Try as ID first
            if str(session_id_or_interaction_id).isdigit():
                return conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id_or_interaction_id,)).fetchone()
            # Try as interaction_id
            return conn.execute("SELECT * FROM sessions WHERE interaction_id = ?", (session_id_or_interaction_id,)).fetchone()

class DeepResearchConfig(BaseModel):

    api_key: str = Field(default_factory=lambda: os.getenv("GEMINI_API_KEY"))
    agent_name: str = "deep-research-pro-preview-12-2025"
    followup_model: str = "gemini-3-pro-preview"

    def __init__(self, **data):
        super().__init__(**data)
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY not found. Please set it in .env or ~/.config/deepresearch/.env")

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
    stores: Optional[List[str]] = None
    stream: bool = False
    output_format: Optional[str] = None
    upload_paths: Optional[List[str]] = None
    output_file: Optional[str] = None

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
    def tools_config(self) -> Optional[List[dict]]:
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

    def create_store_from_paths(self, paths: List[str]) -> str:
        print(f"[INFO] Uploading {len(paths)} items to a new File Search Store...")
        store = self.client.file_search_stores.create()
        self.created_stores.append(store.name)
        print(f"[INFO] Created temporary store: {store.name}")

        for path in paths:
            if os.path.isdir(path):
                for f in os.listdir(path):
                    full_path = os.path.join(path, f)
                    if os.path.isfile(full_path):
                        self._upload_file(full_path, store.name)
            elif os.path.isfile(path):
                self._upload_file(path, store.name)
            else:
                print(f"[WARN] Skipped invalid path: {path}")

        print("[INFO] Waiting 5s for file ingestion...")
        time.sleep(5)
        return store.name

    def _upload_file(self, path: str, store_name: str):
        print(f"[INFO] Uploading: {path}")
        try:
            if hasattr(self.client.file_search_stores, 'upload_to_file_search_store'):
                 self.client.file_search_stores.upload_to_file_search_store(
                     file_search_store_name=store_name,
                     file=path
                 )
                 # Note: We can't easily track the resulting file resource name from this helper 
                 # to delete it individually later. We rely on store deletion.
            else:
                 # Fallback path if helper missing
                 file_obj = self.client.files.upload(path=path)
                 self.uploaded_files.append(file_obj.name)
                 
                 # Wait for processing with 5-minute timeout
                 start_time = time.time()
                 while file_obj.state.name == "PROCESSING":
                     if time.time() - start_time > 300: # 300 seconds = 5 minutes
                         raise TimeoutError(f"File processing timed out after 5 minutes: {path}")
                     time.sleep(2)
                     file_obj = self.client.files.get(name=file_obj.name)
        except Exception as e:
            print(f"[ERROR] Failed to upload {path}: {e}")
            raise

    def cleanup(self):
        print("\n[INFO] Cleaning up temporary resources...")
        for store_name in self.created_stores:
             try:
                 # 1. Empty the store first
                 if hasattr(self.client.file_search_stores, 'documents'):
                     try:
                         # List documents in the store
                         pager = self.client.file_search_stores.documents.list(parent=store_name)
                         for doc in pager:
                             try:
                                 print(f"[INFO] Deleting document: {doc.name}")
                                 # Force delete to remove chunks/non-empty docs
                                 self.client.file_search_stores.documents.delete(
                                     name=doc.name,
                                     config={'force': True}
                                 )
                             except Exception as e:
                                 print(f"[WARN] Failed to delete document {doc.name}: {e}")
                     except Exception as e:
                         # If listing fails, we might just try deleting the store directly
                         print(f"[WARN] Failed to list documents in {store_name}: {e}")

                 # 2. Delete the store
                 self.client.file_search_stores.delete(name=store_name)
                 print(f"[INFO] Deleted store: {store_name}")
             except Exception as e:
                 if "non-empty" in str(e):
                     print(f"[WARN] Could not delete store {store_name} (contains files). It will persist.")
                 else:
                     print(f"[WARN] Failed to delete store {store_name}: {e}")

        # Note: We don't need to delete 'uploaded_files' via client.files.delete() 
        # if they were uploaded via upload_to_file_search_store() as they are managed by the store?
        # Actually, 'upload_to_file_search_store' creates a Document. 
        # Does it create a File resource too? 
        # Usually yes. But if we delete the Document, does it delete the File?
        # Let's keep the file deletion logic just in case we used the fallback upload method.
        for file_name in self.uploaded_files:
            try:
                self.client.files.delete(name=file_name)
                print(f"[INFO] Deleted file resource: {file_name}")
            except Exception as e:
                pass

class DeepResearchAgent:
    def __init__(self, config: Optional[DeepResearchConfig] = None):
        self.config = config or DeepResearchConfig()
        self.client = genai.Client(api_key=self.config.api_key)
        self.file_manager = FileManager(self.client)
        self.session_manager = SessionManager()

    def _process_stream(self, event_stream, interaction_id_ref: list, last_event_id_ref: list, is_complete_ref: list, request_prompt: str = None, upload_paths: list = None):
        for event in event_stream:
            if event.event_type == "interaction.start":
                interaction_id_ref[0] = event.interaction.id
                print(f"\n[INFO] Interaction started: {event.interaction.id}")
                if request_prompt:
                    self.session_manager.create_session(event.interaction.id, request_prompt, upload_paths)

            if event.event_id:
                last_event_id_ref[0] = event.event_id
            if event.event_type == "content.delta":
                if event.delta.type == "text":
                    print(event.delta.text, end="", flush=True)
                elif event.delta.type == "thought_summary":
                    print(f"\n[THOUGHT] {event.delta.content.text}", flush=True)
            if event.event_type in ['interaction.complete', 'error']:
                is_complete_ref[0] = True

    def start_research_stream(self, request: ResearchRequest):
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
                print(f"[ERROR] File upload failed: {e}")
                self.file_manager.cleanup()
                return None

        last_event_id = [None]
        interaction_id = [None]
        is_complete = [False]

        if request.stores:
            print(f"[INFO] Using File Search Stores: {request.stores}")

        try:
            print("[INFO] Starting Research Stream...")
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
            self._process_stream(initial_stream, interaction_id, last_event_id, is_complete, request.prompt, request.upload_paths)
            
            # Reconnection Loop
            while not is_complete[0] and interaction_id[0]:
                print(f"\n[INFO] Connection lost. Resuming from {last_event_id[0]}...")
                time.sleep(2)
                try:
                    resume_stream = self.client.interactions.get(
                        id=interaction_id[0], stream=True, last_event_id=last_event_id[0]
                    )
                    self._process_stream(resume_stream, interaction_id, last_event_id, is_complete)
                except Exception as e:
                    print(f"[ERROR] Reconnection failed: {e}")
            
            if is_complete[0]:
                 print("\n[INFO] Research Complete.")
                 
                 # Retrieve final text
                 if interaction_id[0]:
                     try:
                         final_interaction = self.client.interactions.get(id=interaction_id[0])
                         if final_interaction.outputs:
                             final_text = final_interaction.outputs[-1].text
                             self.session_manager.update_session(interaction_id[0], "completed", final_text)
                             if request.output_file:
                                 DataExporter.export(final_text, request.output_file)
                     except Exception as e:
                         print(f"[WARN] Failed to retrieve/export result: {e}")

        except KeyboardInterrupt:
            print("\n[WARN] Research interrupted by user.")
        except Exception as e:
            print(f"\n[ERROR] Research failed: {e}")
        finally:
            if request.upload_paths:
                self.file_manager.cleanup()
        
        return interaction_id[0]

    def start_research_poll(self, request: ResearchRequest):
        if request.upload_paths:
             try:
                store_name = self.file_manager.create_store_from_paths(request.upload_paths)
                if request.stores is None: request.stores = []
                request.stores.append(store_name)
             except Exception as e:
                print(f"[ERROR] Upload failed: {e}")
                self.file_manager.cleanup()
                return

        if request.stores:
             print(f"[INFO] Using Stores: {request.stores}")

        print("[INFO] Starting Research (Polling)...")
        try:
            interaction = self.client.interactions.create(
                input=request.final_prompt,
                agent=self.config.agent_name,
                background=True,
                tools=request.tools_config
            )
            print(f"[INFO] Started: {interaction.id}")
            self.session_manager.create_session(interaction.id, request.prompt, request.upload_paths)

            while True:
                interaction = self.client.interactions.get(interaction.id)
                if interaction.status == "completed":
                    print("\n" + "="*40 + " REPORT " + "="*40)
                    final_text = interaction.outputs[-1].text
                    print(final_text)
                    
                    self.session_manager.update_session(interaction.id, "completed", final_text)
                    if request.output_file:
                        DataExporter.export(final_text, request.output_file)
                    break
                elif interaction.status == "failed":
                    print(f"[ERROR] Failed: {interaction.error}")
                    self.session_manager.update_session(interaction.id, "failed")
                    break
                sys.stdout.write(".")
                sys.stdout.flush()
                time.sleep(10)
        except KeyboardInterrupt:
            print("\n[WARN] Polling interrupted by user.")
        except Exception as e:
            print(f"[ERROR] Unexpected error: {e}")
        finally:
            if request.upload_paths:
                self.file_manager.cleanup()
        return interaction.id

    def follow_up(self, request: FollowUpRequest):
        print(f"[INFO] Sending follow-up to interaction: {request.interaction_id}")
        interaction = self.client.interactions.create(
            input=request.prompt,
            model=self.config.followup_model, 
            previous_interaction_id=request.interaction_id
        )
        print(interaction.outputs[-1].text)

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

4. Follow-up Question:
   %(prog)s followup v1_abc123... "Can you elaborate on point 2?"

5. Manage History:
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
    parser_research.add_argument("--stream", action="store_true", help="Stream the agent's thought process (Recommended)")
    parser_research.add_argument("--stores", nargs="+", help="Existing Cloud File Search Store names (advanced)")
    parser_research.add_argument("--upload", nargs="+", help="Local file/folder paths to upload, analyze, and auto-delete")
    parser_research.add_argument("--format", help="Specific output instructions (e.g., 'Technical Report', 'CSV')")
    parser_research.add_argument("--output", help="Save report to file (e.g., report.md, data.json)")

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

    args = parser.parse_args()

    try:
        if args.command == "research":
            request = ResearchRequest(
                prompt=args.prompt,
                stores=args.stores,
                stream=args.stream,
                output_format=args.format,
                upload_paths=args.upload,
                output_file=args.output
            )
            agent = DeepResearchAgent()
            
            if request.stream:
                agent.start_research_stream(request)
            else:
                agent.start_research_poll(request)

        elif args.command == "followup":
            request = FollowUpRequest(
                interaction_id=args.id,
                prompt=args.prompt
            )
            agent = DeepResearchAgent()
            agent.follow_up(request)

        elif args.command == "list":
            mgr = SessionManager()
            sessions = mgr.list_sessions(args.limit)
            print(f"{'ID':<4} | {'Status':<10} | {'Date':<20} | {'Prompt'}")
            print("-" * 80)
            for s in sessions:
                # Truncate prompt
                prompt = s['prompt'].replace('\n', ' ')
                if len(prompt) > 40: prompt = prompt[:37] + "..."
                print(f"{s['id']:<4} | {s['status']:<10} | {s['created_at'][:19]:<20} | {prompt}")

        elif args.command == "show":
            mgr = SessionManager()
            session = mgr.get_session(args.id)
            if not session:
                print(f"[ERROR] Session '{args.id}' not found.")
            else:
                print(f"Session ID: {session['id']}")
                print(f"Interaction ID: {session['interaction_id']}")
                print(f"Date: {session['created_at']}")
                print(f"Status: {session['status']}")
                print(f"Files: {session['files']}")
                print("-" * 40)
                print(f"Prompt:\n{session['prompt']}\n")
                print("-" * 40)
                print("Result:")
                if session['result']:
                    print(session['result'])
                else:
                    print("(No result stored)")

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
                

        