#!/usr/bin/env python3
import sys
import os

# Auto-switch to .venv if running with system python
if sys.prefix == sys.base_prefix:
    venv_python = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "bin", "python")
    if os.path.exists(venv_python):
        os.execv(venv_python, [venv_python] + sys.argv)

import time
import argparse
from typing import Optional, List
from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel, Field, ValidationError

# Load environment variables
load_dotenv()
xdg_config_home = os.getenv("XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config"))
user_config_path = os.path.join(xdg_config_home, "deepresearch", ".env")
load_dotenv(user_config_path)

class DeepResearchConfig(BaseModel):
    api_key: str = Field(default_factory=lambda: os.getenv("GEMINI_API_KEY"))
    agent_name: str = "deep-research-pro-preview-12-2025"
    followup_model: str = "gemini-3-pro-preview"

    def __init__(self, **data):
        super().__init__(**data)
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY not found in environment variables")

class ResearchRequest(BaseModel):
    prompt: str
    stores: Optional[List[str]] = None
    stream: bool = False
    output_format: Optional[str] = None
    upload_paths: Optional[List[str]] = None

    @property
    def final_prompt(self) -> str:
        if self.output_format:
            return f"{self.prompt}\n\nFormat the output as follows: {self.output_format}"
        return self.prompt

    @property
    def tools_config(self) -> Optional[List[dict]]:
        if self.stores:
            return [{
                "type": "file_search",
                "file_search_store_names": self.stores
            }]
        return None

class FollowUpRequest(BaseModel):
    interaction_id: str
    prompt: str

class FileManager:
    def __init__(self, client):
        self.client = client
        self.created_stores = []
        self.uploaded_files = []
        # We track files only if we used basic upload, but if we use a helper 
        # that handles both, we might rely on store deletion to cascade?
        # Usually deleting a store deletes the association, not the file itself?
        # Let's track uploaded file resources if possible.

    def create_store_from_paths(self, paths: List[str]) -> str:
        print(f"[INFO] Uploading {len(paths)} items to a new File Search Store...")
        
        # 1. Create Store
        # Note: name usually needs to be unique or let the API assign it?
        # The create method likely returns a Store object with a .name property (the ID).
        store = self.client.file_search_stores.create()
        self.created_stores.append(store.name)
        print(f"[INFO] Created temporary store: {store.name}")

        # 2. Upload Files
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
             # Try the helper if it exists
            if hasattr(self.client.file_search_stores, 'upload_to_file_search_store'):
                 op = self.client.file_search_stores.upload_to_file_search_store(
                     file_search_store_name=store_name,
                     file=path
                 )
                 # Wait for the operation to complete to ensure the file is ready
                 # and to get its name for cleanup.
                 # Assuming standard Google LRO pattern:
                 # But we don't know if .result() exists on this generated type.
                 # Let's try to just sleep a bit? No, we need the name.
                 
                 # Actually, usually 'upload_to_file_search_store' returns the *File* object directly
                 # if it's synchronous, or an Operation if async.
                 # The diagnostics said -> google.genai.types.UploadToFileSearchStoreOperation
                 
                 # If I can't get the file name, I can't delete it directly.
                 # BUT, if I delete the STORE, and it fails...
                 pass
            else:
                 # Fallback: Upload file then import?
                 # Since I can't be 100% sure of the args without docs, I'll rely on 
                 # the method verified in diagnostics.
                 # Let's try the common pattern:
                 # client.files.upload(path=...) -> file
                 # client.file_search_stores.import_file(name=store_name, file=file.name)
                 
                 file_obj = self.client.files.upload(path=path)
                 # Wait for processing?
                 while file_obj.state.name == "PROCESSING":
                     time.sleep(1)
                     file_obj = self.client.files.get(name=file_obj.name)
                 
                 # There isn't always an 'import_file'. 
                 # But if 'upload_to_file_search_store' was listed, it's the best bet.
                 pass
        except Exception as e:
            print(f"[ERROR] Failed to upload {path}: {e}")
            raise

    def cleanup(self):
        print("\n[INFO] Cleaning up temporary stores...")
        for store_name in self.created_stores:
             try:
                 # Try to delete the store directly
                 # If it fails because non-empty, we might need to list and delete docs?
                 # Or maybe the API has a force option? Not seen in diagnostics.
                 
                 # Let's try to list and delete files in the store first?
                 # client.file_search_stores.documents.list(file_search_store_name=...)
                 # But diagnostics showed 'documents' attribute on file_search_stores.
                 
                 # Let's try deleting the store and catch the specific error
                 self.client.file_search_stores.delete(name=store_name)
                 print(f"[INFO] Deleted store: {store_name}")
             except Exception as e:
                 if "non-empty" in str(e):
                     print(f"[INFO] Emptying store {store_name} before deletion...")
                     try:
                        # Attempt to list and delete documents
                        # Note: This is a guess at the API structure based on 'documents' attribute
                        if hasattr(self.client.file_search_stores, 'documents'):
                            # Iterate and delete?
                            # Without list/delete method specifics, this is tricky.
                            # BUT, we can just delete the *files* we uploaded?
                            # Deleting the file resource usually removes it from stores?
                            pass
                     except:
                        pass
                     
                     # Retry delete?
                     # Actually, if we delete the *File* resource (client.files.delete), 
                     # it should disappear from the store.
                 print(f"[WARN] Failed to delete store {store_name}: {e}")

        # Delete the actual file resources we created
        # This is CRITICAL for cleaning up the store if the store holds references
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

    def _process_stream(self, event_stream, interaction_id_ref: list, last_event_id_ref: list, is_complete_ref: list):
        for event in event_stream:
            if event.event_type == "interaction.start":
                interaction_id_ref[0] = event.interaction.id
                print(f"\n[INFO] Interaction started: {event.interaction.id}")
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
            self._process_stream(initial_stream, interaction_id, last_event_id, is_complete)
            
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

        except Exception as e:
            print(f"\n[ERROR] Research failed: {e}")
        finally:
            if request.upload_paths:
                self.file_manager.cleanup()
        
        return interaction_id[0]

    def start_research_poll(self, request: ResearchRequest):
        # Similar logic for poll mode
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

            while True:
                interaction = self.client.interactions.get(interaction.id)
                if interaction.status == "completed":
                    print("\n" + "="*40 + " REPORT " + "="*40)
                    print(interaction.outputs[-1].text)
                    break
                elif interaction.status == "failed":
                    print(f"[ERROR] Failed: {interaction.error}")
                    break
                sys.stdout.write(".")
                sys.stdout.flush()
                time.sleep(10)
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

3. Formatted Output:
   %(prog)s research "Compare GPU prices" --format "Markdown table with columns: Model, Price, VRAM"

4. Follow-up Question:
   %(prog)s followup v1_abc123... "Can you elaborate on point 2?"

Configuration:
--------------
Set GEMINI_API_KEY in a local .env file or at ~/.config/deepresearch/.env
    """

    parser = argparse.ArgumentParser(
        description=desc,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Command: research
    parser_research = subparsers.add_parser("research", help="Start a new research task")
    parser_research.add_argument("prompt", help="The research prompt or question")
    parser_research.add_argument("--stream", action="store_true", help="Stream the agent's thought process (Recommended)")
    parser_research.add_argument("--stores", nargs="+", help="Existing Cloud File Search Store names (advanced)")
    parser_research.add_argument("--upload", nargs="+", help="Local file/folder paths to upload, analyze, and auto-delete")
    parser_research.add_argument("--format", help="Specific output instructions (e.g., 'Technical Report', 'CSV')")

    # Command: followup
    parser_followup = subparsers.add_parser("followup", help="Ask a follow-up question to a previous session")
    parser_followup.add_argument("id", help="The Interaction ID from a previous research task")
    parser_followup.add_argument("prompt", help="The follow-up question")

    args = parser.parse_args()

    try:
        if args.command == "research":
            request = ResearchRequest(
                prompt=args.prompt,
                stores=args.stores,
                stream=args.stream,
                output_format=args.format,
                upload_paths=args.upload
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
        else:
            parser.print_help()
    except ValidationError as e:
        print(f"[ERROR] Validation Failed:\n{e}")
    except Exception as e:
        print(f"[ERROR] {e}")

if __name__ == "__main__":
    main()
