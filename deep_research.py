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
__version__ = "0.12.0"

def get_version():
    try:
        return version("deepresearch")
    except PackageNotFoundError:
        return __version__


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
    def save_json(content: str, filepath: str, console: Console):
        try:
            # Try to extract JSON from code blocks first
            clean_content = DataExporter.extract_code_block(content, "json")
            # Attempt to parse to ensure validity
            data = json.loads(clean_content)
            
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=2)
            console.print(f"[INFO] JSON report saved to {filepath}")
        except json.JSONDecodeError as e:
            console.print(f"[ERROR] Failed to parse JSON output: {e}")
            # Save raw content as fallback
            with open(filepath + ".raw", 'w') as f:
                f.write(content)
            console.print(f"[WARN] Raw content saved to {filepath}.raw")

    @staticmethod
    def save_csv(content: str, filepath: str, console: Console):
        try:
            clean_content = DataExporter.extract_code_block(content, "csv")
            with open(filepath, 'w') as f:
                f.write(clean_content)
            console.print(f"[INFO] CSV report saved to {filepath}")
        except Exception as e:
            console.print(f"[ERROR] Failed to save CSV: {e}")

    @staticmethod
    def export(content: str, filepath: str, console: Console):
        if filepath.lower().endswith('.json'):
            DataExporter.save_json(content, filepath, console)
        elif filepath.lower().endswith('.csv'):
            DataExporter.save_csv(content, filepath, console)
        else:
            # Default text/markdown save
            with open(filepath, 'w') as f:
                f.write(content)
            console.print(f"[INFO] Report saved to {filepath}")

class ResearchRequest(BaseModel):
    prompt: str
    stores: list[str] | None = None
    verbose: bool = False
    output_format: str | None = None
    upload_paths: list[str] | None = None
    output_file: str | None = None
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


class FileManager:
    def __init__(self, client, console: Console):
        self.client = client
        self.console = console
        self.created_stores = []
        self.uploaded_files = []

    def create_store_from_paths(self, paths: list[str]) -> str:
        self.console.print(f"[bold cyan][INFO][/] Uploading {len(paths)} items to a new File Search Store...")
        store = self.client.file_search_stores.create()
        self.created_stores.append(store.name)
        self.console.print(f"[bold cyan][INFO][/] Created temporary store: {store.name}")

        for path in paths:
            if os.path.isdir(path):
                for f in os.listdir(path):
                    full_path = os.path.join(path, f)
                    if os.path.isfile(full_path):
                        self._upload_file(full_path, store.name)
            elif os.path.isfile(path):
                self._upload_file(path, store.name)
            else:
                self.console.print(f"[bold yellow][WARN][/] Skipped invalid path: {path}")

        self.console.print("[bold cyan][INFO][/] Waiting 5s for file ingestion...")
        time.sleep(5)
        return store.name

    def _upload_file(self, path: str, store_name: str):
        self.console.print(f"[bold cyan][INFO][/] Uploading: {path}")
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
            self.console.print(f"[bold red][ERROR][/] Failed to upload {path}: {e}")
            raise

    def cleanup(self):
        self.console.print("\n[bold cyan][INFO][/] Cleaning up temporary resources...")
        for store_name in self.created_stores:
             try:
                 # 1. Empty the store first
                 if hasattr(self.client.file_search_stores, 'documents'):
                     try:
                         # List documents in the store
                         pager = self.client.file_search_stores.documents.list(parent=store_name)
                         for doc in pager:
                             try:
                                 self.console.print(f"[bold cyan][INFO][/] Deleting document: {doc.name}")
                                 # Force delete to remove chunks/non-empty docs
                                 self.client.file_search_stores.documents.delete(
                                     name=doc.name,
                                     config={'force': True}
                                 )
                             except Exception as e:
                                 self.console.print(f"[bold yellow][WARN][/] Failed to delete document {doc.name}: {e}")
                     except Exception as e:
                         # If listing fails, we might just try deleting the store directly
                         self.console.print(f"[bold yellow][WARN][/] Failed to list documents in {store_name}: {e}")

                 # 2. Delete the store
                 self.client.file_search_stores.delete(name=store_name)
                 self.console.print(f"[bold cyan][INFO][/] Deleted store: {store_name}")
             except Exception as e:
                 if "non-empty" in str(e):
                     self.console.print(f"[bold yellow][WARN][/] Could not delete store {store_name} (contains files). It will persist.")
                 else:
                     self.console.print(f"[bold yellow][WARN][/] Failed to delete store {store_name}: {e}")

        # Note: We don't need to delete 'uploaded_files' via client.files.delete() 
        # if they were uploaded via upload_to_file_search_store() as they are managed by the store?
        # Actually, 'upload_to_file_search_store' creates a Document. 
        # Does it create a File resource too? 
        # Usually yes. But if we delete the Document, does it delete the File?
        # Let's keep the file deletion logic just in case we used the fallback upload method.
        for file_name in self.uploaded_files:
            try:
                self.client.files.delete(name=file_name)
                self.console.print(f"[bold cyan][INFO][/] Deleted file resource: {file_name}")
            except Exception:
                pass

class DeepResearchAgent:
    def __init__(self, console: Console, config: DeepResearchConfig | None = None):
        self.config = config or DeepResearchConfig()
        self.client = genai.Client(api_key=self.config.api_key)
        self.console = console
        self.file_manager = FileManager(self.client, self.console)

    def _process_stream(self, event_stream, interaction_id_ref: list, last_event_id_ref: list, is_complete_ref: list):
        for event in event_stream:
            if event.event_type == "interaction.start":
                interaction_id_ref[0] = event.interaction.id
                self.console.print(f"\n[INFO] Interaction started: {event.interaction.id}")

            if event.event_id:
                last_event_id_ref[0] = event.event_id
            if event.event_type == "content.delta":
                if event.delta.type == "text":
                    self.console.print(event.delta.text, end="")
                elif event.delta.type == "thought_summary":
                    self.console.print(f"\n[THOUGHT] {event.delta.content.text}", flush=True)
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
                self.console.print(f"[ERROR] File upload failed: {e}")
                self.file_manager.cleanup()
                return None

        last_event_id = [None]
        interaction_id = [None]
        is_complete = [False]

        if request.stores:
            self.console.print(f"[INFO] Using File Search Stores: {request.stores}")

        try:
            self.console.print("[INFO] Starting Research Stream...")
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
            self._process_stream(initial_stream, interaction_id, last_event_id, is_complete)
            
            # Reconnection Loop
            while not is_complete[0] and interaction_id[0]:
                self.console.print(f"\n[INFO] Connection lost. Resuming from {last_event_id[0]}...")
                time.sleep(2)
                try:
                    resume_stream = self.client.interactions.get(
                        id=interaction_id[0], stream=True, last_event_id=last_event_id[0]
                    )
                    self._process_stream(resume_stream, interaction_id, last_event_id, is_complete)
                except Exception as e:
                    self.console.print(f"[ERROR] Reconnection failed: {e}")
            
            if is_complete[0]:
                self.console.print("\n[INFO] Research Complete.")

                # Retrieve final text
                if interaction_id[0]:
                    try:
                        final_interaction = self.client.interactions.get(id=interaction_id[0])
                        if final_interaction.outputs:
                            final_text = final_interaction.outputs[-1].text
                            if request.output_file:
                                DataExporter.export(final_text, request.output_file, self.console)
                            return final_text
                    except Exception as e:
                        self.console.print(f"[WARN] Failed to retrieve/export result: {e}")

        except KeyboardInterrupt:
            self.console.print("\n[WARN] Research interrupted by user.")
        except Exception as e:
            self.console.print(f"\n[ERROR] Research failed: {e}")
        finally:
            if request.upload_paths:
                self.file_manager.cleanup()
        
        return None

    def start_research_poll(self, request: ResearchRequest):
        if request.upload_paths:
             try:
                store_name = self.file_manager.create_store_from_paths(request.upload_paths)
                if request.stores is None:
                    request.stores = []
                request.stores.append(store_name)
             except Exception as e:
                self.console.print(f"[ERROR] Upload failed: {e}")
                self.file_manager.cleanup()
                return

        if request.stores:
             self.console.print(f"[INFO] Using Stores: {request.stores}")

        self.console.print("[INFO] Starting Research (Polling)...")
        try:
            interaction = self.client.interactions.create(
                input=request.final_prompt,
                agent=self.config.agent_name,
                background=True,
                tools=request.tools_config
            )
            self.console.print(f"[INFO] Started: {interaction.id}")

            while True:
                interaction = self.client.interactions.get(interaction.id)
                if interaction.status == "completed":
                    self.console.print("\n[INFO] Research Complete.")
                    final_text = interaction.outputs[-1].text
                    
                    if request.output_file:
                        DataExporter.export(final_text, request.output_file, self.console)
                    return final_text
                elif interaction.status == "failed":
                    error_msg = f"API Error: {interaction.error}"
                    self.console.print(f"[ERROR] Failed: {interaction.error}")
                    break

                self.console.print(".", end="")
                time.sleep(10)
        except KeyboardInterrupt:
            self.console.print("\n[WARN] Polling interrupted by user.")
        except Exception as e:
            self.console.print(f"[ERROR] Unexpected error: {e}")
        finally:
            if request.upload_paths:
                self.file_manager.cleanup()
        return None

    def analyze_gaps(self, original_prompt: str, report_text: str, limit: int = 3) -> list[str]:
        self.console.print(f"[THOUGHT] Analyzing report for gaps (Limit: {limit})...")
        
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
            self.console.print("[DEBUG] Sending gap analysis request...")
            response = self.client.models.generate_content(
                model=self.config.followup_model,
                contents=prompt
            )
            text = response.text
            self.console.print(f"[DEBUG] Gap analysis response: {text[:100]}...")
            
            # Use DataExporter utility to extract JSON
            json_str = DataExporter.extract_code_block(text, "json")
            if not json_str:
                # If extraction fails, assume no gaps or parsing error
                # We could try regex finding simple list items but JSON is safer
                return []
            return json.loads(json_str)
        except Exception as e:
            self.console.print(f"[WARN] Failed to analyze gaps: {e}")
            return []

    def synthesize_findings(self, original_prompt: str, main_report: str, sub_reports: list[str]) -> str:
        self.console.print(f"[THOUGHT] Synthesizing {len(sub_reports)} child reports into final answer...")
        
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
            self.console.print(f"[ERROR] Synthesis failed: {e}")
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
            self.console.print("[INFO] Recursive Research Complete.")
            if request.output_file:
                 DataExporter.export(final_result, request.output_file, self.console)
            return final_result
        return None

    def _execute_recursion_level(self, prompt: str, current_depth: int, max_depth: int, breadth: int, original_request: ResearchRequest) -> str | None:
        indent = "  " * (current_depth - 1)
        if current_depth > 1:
            self.console.print(f"{indent}[INFO] Recursive Step Depth {current_depth}/{max_depth}: {prompt}")
        else:
            self.console.print(f"[INFO] Starting Recursive Research (Depth {max_depth}, Breadth {breadth})")

        # 1. Prepare Request
        node_req = ResearchRequest(
            prompt=prompt,
            upload_paths=original_request.upload_paths,
            stores=original_request.stores,
            verbose=(current_depth == 1 and original_request.verbose), # Stream Root only if verbose
            depth=current_depth
        )

        # 2. Execute Research
        if current_depth == 1 and original_request.verbose:
             report = self.start_research_stream(node_req)
        else:
             report = self.start_research_poll(node_req)

        # 3. Check Result
        if not report:
            self.console.print(f"{indent}[ERROR] Research failed or incomplete.")
            return None
        
        self.console.print(f"{indent}[INFO] Phase 1 complete. Report length: {len(report)} chars.")

        # 4. Check Termination
        if current_depth >= max_depth:
            return report

        # 5. Analyze Gaps
        self.console.print(f"{indent}[INFO] Analyzing gaps...")
        questions = self.analyze_gaps(prompt, report, limit=breadth)
        self.console.print(f"{indent}[INFO] Gaps found: {len(questions)}")
        
        if not questions:
            return report

        self.console.print(f"{indent}[INFO] Spawning {len(questions)} sub-tasks...")

        # 6. Recurse in Parallel
        sub_reports = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=breadth) as executor:
            futures = []
            for q in questions:
                futures.append(executor.submit(
                    self._run_recursive_child_safe, 
                    q, current_depth + 1, max_depth, breadth, original_request
                ))
            
            # Enforce timeout
            done, not_done = concurrent.futures.wait(futures, timeout=self.config.recursion_timeout)
            
            for f in done:
                try:
                    res = f.result()
                    if res:
                        sub_reports.append(res)
                except Exception as e:
                    self.console.print(f"{indent}[WARN] Child failed: {e}")
            
            if not_done:
                self.console.print(f"{indent}[ERROR] {len(not_done)} child tasks timed out.")

        if not sub_reports:
            return report

        # 7. Synthesis
        final_report = self.synthesize_findings(prompt, report, sub_reports)
        return final_report

    def _run_recursive_child_safe(self, q, d, max_d, b, req):
        # Helper to instantiate agent and run recursion in thread
        # Create a new console that is silenced if the parent is silenced
        child_console = Console(width=120, quiet=self.console.quiet)
        agent = DeepResearchAgent(console=child_console, config=self.config)
        return agent._execute_recursion_level(q, d, max_d, b, req)

def main():
    desc = "A powerful tool to conduct autonomous, multi-step research using Gemini 3 Pro."
    epilog = """
Examples:
---------
1. Basic Web Research:
   %(prog)s "History of the internet"

2. Research with Local Files (Smart Context):
   %(prog)s "Summarize this contract" --upload ./contract.pdf

3. Formatted Output & Export:
   %(prog)s "Compare GPU prices" --format "Markdown table" --output prices.md
   %(prog)s "List top 5 cloud providers" --output market_data.json

4. Show Agent's Thought Process:
   %(prog)s "How do LLMs work?" --verbose

Configuration:
--------------
Set GEMINI_API_KEY in a local .env file or at ~/.config/deepresearch/.env
    """

    parser = argparse.ArgumentParser(
        description=desc,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument("prompt", help="The research prompt or question")
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s {get_version()}")
    parser.add_argument("--verbose", action="store_true", help="Stream the agent's thought process")
    parser.add_argument("--stores", nargs="+", help="Existing Cloud File Search Store names (advanced)")
    parser.add_argument("--upload", nargs="+", help="Local file/folder paths to upload, analyze, and auto-delete")
    parser.add_argument("--format", help="Specific output instructions (e.g., 'Technical Report', 'CSV')")
    parser.add_argument("--output", help="Save report to file (e.g., report.md, data.json)")
    parser.add_argument("--depth", type=int, default=1, help="Recursive research depth (default: 1)")
    parser.add_argument("--breadth", type=int, default=3, help="Max child tasks per recursion level (default: 3)")

    args = parser.parse_args()

    # Initialize console based on verbosity
    # All agent output is sent here. If quiet, nothing is printed until the final result.
    output_console = Console(width=120, quiet=not args.verbose)
    try:
        request = ResearchRequest(
            prompt=args.prompt,
            stores=args.stores,
            verbose=args.verbose,
            output_format=args.format,
            upload_paths=args.upload,
            output_file=args.output,
            depth=args.depth,
            breadth=args.breadth
        )

        agent = DeepResearchAgent(console=output_console)
        final_report = None

        if request.depth > 1:
            if request.verbose:
                output_console.print("[INFO] Recursive research does not support streaming to stdout. Thought process will be logged instead.")
            final_report = agent.start_recursive_research(request)
        elif request.verbose:
            final_report = agent.start_research_stream(request)
        else:
            final_report = agent.start_research_poll(request)

        # Print the final report to stdout if not in verbose/streaming mode
        if final_report and not request.verbose:
            # Use a non-quiet console to ensure final output is always visible
            final_console = Console(width=120)
            final_console.print(Markdown(final_report))
    except ValidationError as e:
        print(f"[ERROR] Input Validation Failed:\n{e}")
    except ValueError as e:
        print(f"[CONFIG ERROR] {e}")
    except Exception as e:
        print(f"[CRITICAL ERROR] {e}")

if __name__ == "__main__":
    main()
                

        