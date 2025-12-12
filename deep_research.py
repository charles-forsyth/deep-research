import os
import time
import argparse
import sys
from typing import Optional, List
from dotenv import load_dotenv
from google import genai
from google.genai import types

# Load environment variables
load_dotenv()

class DeepResearchAgent:
    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY not found in .env file")
        
        self.client = genai.Client(api_key=self.api_key)
        self.agent_name = "deep-research-pro-preview-12-2025"

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

    def start_research_stream(self, prompt: str, stores: Optional[List[str]] = None):
        """Starts research with streaming and reconnection logic."""
        
        tools = []
        if stores:
            print(f"[INFO] Using File Search Stores: {stores}")
            tools.append({
                "type": "file_search",
                "file_search_store_names": stores
            })

        agent_config = {
            "type": "deep-research",
            "thinking_summaries": "auto"
        }

        # State tracking (using lists for mutable references in helper)
        last_event_id = [None]
        interaction_id = [None]
        is_complete = [False]

        # 1. Attempt initial streaming request
        try:
            print("[INFO] Starting Research Stream...")
            initial_stream = self.client.interactions.create(
                input=prompt,
                agent=self.agent_name,
                background=True,
                stream=True,
                tools=tools if tools else None,
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

    def start_research_poll(self, prompt: str, stores: Optional[List[str]] = None):
        """Starts research in background and polls for completion."""
        tools = []
        if stores:
             tools.append({
                "type": "file_search",
                "file_search_store_names": stores
            })

        print("[INFO] Starting Research (Polling Mode)...")
        interaction = self.client.interactions.create(
            input=prompt,
            agent=self.agent_name,
            background=True,
            tools=tools if tools else None
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

    def follow_up(self, prompt: str, previous_interaction_id: str):
        """Sends a follow-up question to a completed interaction."""
        print(f"[INFO] Sending follow-up to interaction: {previous_interaction_id}")
        
        # Note: Follow-ups use the standard model, not the deep research agent explicitly
        # according to the docs, but context implies we are chatting with the result.
        # The docs say: model="gemini-3-pro-preview" for follow up.
        
        interaction = self.client.interactions.create(
            input=prompt,
            model="gemini-3-pro-preview", 
            previous_interaction_id=previous_interaction_id
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
        agent = DeepResearchAgent()

        if args.command == "research":
            final_prompt = args.prompt
            if args.format:
                final_prompt += f"\n\nFormat the output as follows: {args.format}"
            
            if args.stream:
                agent.start_research_stream(final_prompt, args.stores)
            else:
                agent.start_research_poll(final_prompt, args.stores)

        elif args.command == "followup":
            agent.follow_up(args.prompt, args.id)

        else:
            parser.print_help()

    except Exception as e:
        print(f"[ERROR] {e}")

if __name__ == "__main__":
    main()
