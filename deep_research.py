#!/usr/bin/env python3
import os
import time
import argparse
import sys
from typing import Optional, List
from dotenv import load_dotenv
from google import genai
from pydantic import BaseModel, Field, ValidationError

# Load environment variables
load_dotenv()

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

class DeepResearchAgent:
    def __init__(self, config: Optional[DeepResearchConfig] = None):
        self.config = config or DeepResearchConfig()
        self.client = genai.Client(api_key=self.config.api_key)

    def _process_stream(self, event_stream, interaction_id_ref: list, last_event_id_ref: list, is_complete_ref: list):
        """Helper to process events from any stream source."""
        for event in event_stream:
            # Capture Interaction ID
            if event.event_type == "interaction.start":
                interaction_id_ref[0] = event.interaction.id
                print(f"\n[INFO] Interaction started: {event.interaction.id}")

            # Capture Event ID
            if event.event_id:
                last_event_id_ref[0] = event.event_id

            # Print content
            if event.event_type == "content.delta":
                if event.delta.type == "text":
                    print(event.delta.text, end="", flush=True)
                elif event.delta.type == "thought_summary":
                    print(f"\n[THOUGHT] {event.delta.content.text}", flush=True)

            # Check completion
            if event.event_type in ['interaction.complete', 'error']:
                is_complete_ref[0] = True

    def start_research_stream(self, request: ResearchRequest):
        """Starts research with streaming and reconnection logic."""
        
        agent_config = {
            "type": "deep-research",
            "thinking_summaries": "auto"
        }

        # State tracking (using lists for mutable references in helper)
        last_event_id = [None]
        interaction_id = [None]
        is_complete = [False]

        # 1. Attempt initial streaming request
        if request.stores:
            print(f"[INFO] Using File Search Stores: {request.stores}")

        try:
            print("[INFO] Starting Research Stream...")
            initial_stream = self.client.interactions.create(
                input=request.final_prompt,
                agent=self.config.agent_name,
                background=True,
                stream=True,
                tools=request.tools_config,
                agent_config=agent_config
            )
            self._process_stream(initial_stream, interaction_id, last_event_id, is_complete)
        except Exception as e:
            print(f"\n[WARN] Initial connection dropped: {e}")

        # 2. Reconnection Loop
        while not is_complete[0] and interaction_id[0]:
            print(f"\n[INFO] Connection lost/interrupted. Resuming from event {last_event_id[0]}...")
            time.sleep(2)

            try:
                resume_stream = self.client.interactions.get(
                    id=interaction_id[0],
                    stream=True,
                    last_event_id=last_event_id[0]
                )
                self._process_stream(resume_stream, interaction_id, last_event_id, is_complete)
            except Exception as e:
                print(f"[ERROR] Reconnection failed, retrying... ({e})")
        
        if is_complete[0]:
             print("\n[INFO] Research Complete.")
        
        return interaction_id[0]

    def start_research_poll(self, request: ResearchRequest):
        """Starts research in background and polls for completion."""
        
        if request.stores:
            print(f"[INFO] Using File Search Stores: {request.stores}")

        print("[INFO] Starting Research (Polling Mode)...")
        interaction = self.client.interactions.create(
            input=request.final_prompt,
            agent=self.config.agent_name,
            background=True,
            tools=request.tools_config
        )

        print(f"[INFO] Research started: {interaction.id}")

        while True:
            interaction = self.client.interactions.get(interaction.id)
            if interaction.status == "completed":
                print("\n" + "="*40 + " REPORT " + "="*40)
                print(interaction.outputs[-1].text)
                break
            elif interaction.status == "failed":
                print(f"[ERROR] Research failed: {interaction.error}")
                break
            
            sys.stdout.write(".")
            sys.stdout.flush()
            time.sleep(10)
        
        return interaction.id

    def follow_up(self, request: FollowUpRequest):
        """Sends a follow-up question to a completed interaction."""
        print(f"[INFO] Sending follow-up to interaction: {request.interaction_id}")
        
        interaction = self.client.interactions.create(
            input=request.prompt,
            model=self.config.followup_model, 
            previous_interaction_id=request.interaction_id
        )

        print(interaction.outputs[-1].text)

def main():
    parser = argparse.ArgumentParser(description="CLI for Gemini Deep Research Agent")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Command: research
    parser_research = subparsers.add_parser("research", help="Start a new research task")
    parser_research.add_argument("prompt", help="The research prompt or question")
    parser_research.add_argument("--stream", action="store_true", help="Stream the results with thinking summaries (default: polling)")
    parser_research.add_argument("--stores", nargs="+", help="List of File Search Store names (e.g., fileSearchStores/my-store)")
    parser_research.add_argument("--format", help="Optional formatting instructions (e.g., 'technical report')")

    # Command: followup
    parser_followup = subparsers.add_parser("followup", help="Ask a follow-up question")
    parser_followup.add_argument("id", help="The Previous Interaction ID")
    parser_followup.add_argument("prompt", help="The follow-up question")

    args = parser.parse_args()

    try:
        if args.command == "research":
            request = ResearchRequest(
                prompt=args.prompt,
                stores=args.stores,
                stream=args.stream,
                output_format=args.format
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