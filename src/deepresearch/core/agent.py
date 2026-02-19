import sys
import time
import json
import typing
import concurrent.futures
from datetime import datetime
from google import genai

from deepresearch.core.config import DeepResearchConfig
from deepresearch.core.session import SessionManager
from deepresearch.storage.files import FileManager
from deepresearch.cli.base import ResearchRequest, FollowUpRequest
from deepresearch.utils.exporters import DataExporter
from deepresearch.utils.logger import log_message, setup_logger


class DeepResearchAgent:
    def __init__(
        self, config: DeepResearchConfig | None = None, logger=None, quiet: bool = False
    ):
        self.config = config or DeepResearchConfig()
        self.client = genai.Client(api_key=self.config.api_key)
        self.file_manager = FileManager(self.client)
        self.session_manager = SessionManager()
        self.logger = logger or setup_logger(quiet)
        self.quiet = quiet

    def _log(self, message: str, end: str = "\n", **kwargs):
        """Internal logging helper that respects the custom logger."""
        log_message(self.logger, message, end=end, **kwargs)

    def _process_stream(
        self,
        event_stream,
        interaction_id_ref: list,
        last_event_id_ref: list,
        is_complete_ref: list,
        request_prompt: str | None = None,
        upload_paths: list | None = None,
        adopt_session_id: int | None = None,
    ):
        for event in event_stream:
            if event.event_type == "interaction.start":
                interaction_id_ref[0] = event.interaction.id
                self._log(f"\n[INFO] Interaction started: {event.interaction.id}")
                if adopt_session_id:
                    self.session_manager.update_session_interaction_id(
                        adopt_session_id, event.interaction.id
                    )
                elif request_prompt:
                    self.session_manager.create_session(
                        event.interaction.id, request_prompt, upload_paths
                    )

            if event.event_id:
                last_event_id_ref[0] = event.event_id
            if event.event_type == "content.delta":
                if event.delta.type == "text":
                    self._log(event.delta.text, end="")
                elif event.delta.type == "thought_summary":
                    self._log(f"\n[THOUGHT] {event.delta.content.text}", flush=True)
            if event.event_type in ["interaction.complete", "error"]:
                is_complete_ref[0] = True

    def start_research_stream(
        self, request: ResearchRequest, auto_update_status: bool = True
    ):
        agent_config: typing.Dict[str, typing.Any] = {
            "type": "deep-research",
            "thinking_summaries": "auto",
        }

        if request.upload_paths:
            try:
                store_name = self.file_manager.create_store_from_paths(
                    request.upload_paths
                )
                if request.stores is None:
                    request.stores = []
                request.stores.append(store_name)

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
            if not hasattr(self.client, "interactions"):
                import google.genai

                raise RuntimeError(
                    f"google-genai version {google.genai.__version__} too old."
                )

            initial_stream = self.client.interactions.create(
                input=request.final_prompt,
                agent=self.config.agent_name,
                background=True,
                stream=True,
                tools=request.tools_config,
                agent_config=agent_config,
            )  # type: ignore

            self._process_stream(
                initial_stream,
                interaction_id,
                last_event_id,
                is_complete,
                request.prompt,
                request.upload_paths,
                request.adopt_session_id,
            )

            while not is_complete[0] and interaction_id[0]:
                self._log(
                    f"\n[INFO] Connection lost. Resuming from {last_event_id[0]}..."
                )
                time.sleep(2)
                try:
                    resume_stream = self.client.interactions.get(
                        id=interaction_id[0],
                        stream=True,
                        last_event_id=last_event_id[0],
                    )
                    self._process_stream(
                        resume_stream,
                        interaction_id,
                        last_event_id,
                        is_complete,
                        adopt_session_id=request.adopt_session_id,
                    )
                except Exception as e:
                    self._log(f"[ERROR] Reconnection failed: {e}")

            if is_complete[0]:
                self._log("\n[INFO] Research Complete.")

                if interaction_id[0]:
                    try:
                        final_interaction = self.client.interactions.get(
                            id=interaction_id[0]
                        )
                        if final_interaction.outputs:
                            final_text = str(
                                getattr(final_interaction.outputs[-1], "text", "")
                            )

                            if self.quiet:
                                print(final_text)

                            if auto_update_status:
                                self.session_manager.update_session(
                                    interaction_id[0], "completed", final_text
                                )
                            else:
                                self.session_manager.update_session(
                                    interaction_id[0], "running", final_text
                                )

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
                self.session_manager.update_session(
                    interaction_id[0], "failed", result=f"Exception: {e}"
                )
        finally:
            if request.upload_paths:
                self.file_manager.cleanup()

        return interaction_id[0]

    def start_research_poll(
        self, request: ResearchRequest, auto_update_status: bool = True
    ):
        if request.upload_paths:
            try:
                store_name = self.file_manager.create_store_from_paths(
                    request.upload_paths
                )
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
                tools=request.tools_config,  # type: ignore[arg-type]
            )  # type: ignore
            self._log(f"[INFO] Started: {interaction.id}")

            if hasattr(request, "adopt_session_id") and request.adopt_session_id:
                self.session_manager.update_session_interaction_id(
                    request.adopt_session_id, interaction.id
                )
            else:
                self.session_manager.create_session(
                    interaction.id, request.prompt, request.upload_paths
                )

            while True:
                interaction = self.client.interactions.get(interaction.id)
                if interaction.status == "completed":
                    self._log("\n" + "=" * 40 + " REPORT " + "=" * 40)
                    final_text = (
                        str(getattr(interaction.outputs[-1], "text", ""))
                        if interaction.outputs
                        else ""
                    )

                    if len(final_text) > 2000:
                        self._log(
                            final_text[:2000]
                            + "\n\n... [Report Truncated in Logs. Full content in DB] ..."
                        )
                    else:
                        self._log(final_text)

                    if self.quiet:
                        print(final_text)

                    if auto_update_status:
                        self.session_manager.update_session(
                            interaction.id, "completed", final_text
                        )
                    else:
                        self.session_manager.update_session(
                            interaction.id, "running", final_text
                        )

                    if request.output_file:
                        DataExporter.export(final_text, request.output_file)
                    break
                elif interaction.status == "failed":
                    error_msg = f"API Error: {getattr(interaction, 'error', None)}"
                    self._log(f"[ERROR] Failed: {getattr(interaction, 'error', None)}")
                    self.session_manager.update_session(
                        interaction.id, "failed", result=error_msg
                    )
                    break

                # Check quiet vs normal print polling
                if not self.quiet:
                    sys.stdout.write(".")
                    sys.stdout.flush()
                time.sleep(10)
        except KeyboardInterrupt:
            self._log("\n[WARN] Polling interrupted by user.")
            if "interaction" in locals() and hasattr(interaction, "id"):
                self.session_manager.update_session(interaction.id, "cancelled")
        except Exception as e:
            self._log(f"[ERROR] Unexpected error: {e}")
            if "interaction" in locals() and hasattr(interaction, "id"):
                self.session_manager.update_session(
                    interaction.id, "failed", result=f"Exception: {e}"
                )
        finally:
            if request.upload_paths:
                self.file_manager.cleanup()
        return (
            interaction.id
            if "interaction" in locals() and hasattr(interaction, "id")
            else None
        )

    def follow_up(self, request: FollowUpRequest):
        self._log(f"[INFO] Sending follow-up to interaction: {request.interaction_id}")
        try:
            interaction = self.client.interactions.create(
                input=request.prompt,
                model=self.config.followup_model,
                previous_interaction_id=request.interaction_id,
            )
            if interaction.outputs:
                response_text = str(getattr(interaction.outputs[-1], "text", ""))
                self._log(response_text)

                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                append_text = f"\n\n---\n### Follow-up ({timestamp})\n\n**Q: {request.prompt}**\n\n{response_text}"
                self.session_manager.append_to_result(
                    request.interaction_id, append_text
                )
        except Exception as e:
            self._log(f"[ERROR] Follow-up failed: {e}")

    def analyze_gaps(
        self, original_prompt: str, report_text: str, limit: int = 3
    ) -> list[str]:
        self._log(f"[THOUGHT] Analyzing report for gaps (Limit: {limit})...")

        prompt = (
            f"Original Objective: {original_prompt}\n\n"
            f"Report:\n{report_text}\n\n"
            "INSTRUCTIONS:\n"
            "1. Analyze the report against the objective.\n"
            f"2. Identify 1-{limit} critical gaps, unanswered questions, or areas needing deeper verification.\n"
            "3. If the report is comprehensive, return an empty list.\n"
            '4. Output strictly a JSON list of strings, e.g., ["Question 1", "Question 2"].\n'
            "5. Wrap the JSON in a ```json code block."
        )

        try:
            self._log("[DEBUG] Sending gap analysis request...")
            response = self.client.models.generate_content(
                model=self.config.followup_model, contents=prompt
            )
            text = response.text or ""
            self._log(f"[DEBUG] Gap analysis response: {text[:100]}...")

            json_str = DataExporter.extract_code_block(text, "json")
            if not json_str:
                return []
            return json.loads(json_str)
        except Exception as e:
            self._log(f"[WARN] Failed to analyze gaps: {e}")
            return []

    def synthesize_findings(
        self, original_prompt: str, main_report: str, sub_reports: list[str]
    ) -> str:
        self._log(
            f"[THOUGHT] Synthesizing {len(sub_reports)} child reports into final answer..."
        )

        combined_subs = "\n\n---\n\n".join(
            [f"Sub-Report {i + 1}:\n{r}" for i, r in enumerate(sub_reports)]
        )

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
                model=self.config.followup_model, contents=prompt
            )
            return response.text or ""
        except Exception as e:
            self._log(f"[ERROR] Synthesis failed: {e}")
            return (
                main_report
                + "\n\n[ERROR: Synthesis failed. Appending raw sub-reports below]\n\n"
                + combined_subs
            )

    def start_recursive_research(self, request: ResearchRequest):
        final_result = self._execute_recursion_level(
            prompt=request.prompt,
            current_depth=1,
            max_depth=request.depth,
            breadth=request.breadth,
            original_request=request,
        )

        if final_result:
            self._log("[INFO] Recursive Research Complete.")
            sys.stdout.flush()

        if request.output_file and final_result:
            DataExporter.export(final_result, request.output_file)

    def _execute_recursion_level(
        self,
        prompt: str,
        current_depth: int,
        max_depth: int,
        breadth: int,
        original_request: ResearchRequest,
        parent_id: int | None = None,
    ) -> str | None:
        indent = "  " * (current_depth - 1)
        if current_depth > 1:
            self._log(
                f"{indent}[INFO] Recursive Step Depth {current_depth}/{max_depth}: {prompt}"
            )
        else:
            self._log(
                f"[INFO] Starting Recursive Research (Depth {max_depth}, Breadth {breadth})"
            )

        node_req = ResearchRequest(
            prompt=prompt,
            upload_paths=original_request.upload_paths,
            stores=original_request.stores,
            stream=(current_depth == 1),
            depth=current_depth,
        )

        is_leaf = current_depth >= max_depth

        if current_depth == 1:
            interaction_id = self.start_research_stream(
                node_req, auto_update_status=is_leaf
            )
        else:
            child_sid = self.session_manager.create_session(
                "pending_recursion",
                prompt,
                original_request.upload_paths,
                parent_id=parent_id,
                depth=current_depth,
            )
            node_req.adopt_session_id = child_sid
            interaction_id = self.start_research_poll(
                node_req, auto_update_status=is_leaf
            )

        session = self.session_manager.get_session(interaction_id)

        if not session:
            self._log(f"{indent}[ERROR] Research session not found.")
            return None

        status = session["status"]
        result = session["result"]

        if status != "completed" and not result:
            self._log(
                f"{indent}[ERROR] Research failed or incomplete. Status: {status}"
            )
            return None

        report = result
        current_id = session["id"]
        self._log(
            f"{indent}[INFO] Phase 1 complete. Report length: {len(report)} chars."
        )

        if current_depth >= max_depth:
            return report

        self._log(f"{indent}[INFO] Analyzing gaps...")
        questions = self.analyze_gaps(prompt, report, limit=breadth)
        self._log(f"{indent}[INFO] Gaps found: {len(questions)}")

        if not questions:
            self.session_manager.update_session(
                interaction_id, "completed", result=report
            )
            return report

        self._log(f"{indent}[INFO] Spawning {len(questions)} sub-tasks...")

        sub_reports = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=breadth) as executor:
            futures = []
            for q in questions:
                futures.append(
                    executor.submit(
                        self._run_recursive_child_safe,
                        q,
                        current_depth + 1,
                        max_depth,
                        breadth,
                        original_request,
                        current_id,
                    )
                )

            done, not_done = concurrent.futures.wait(
                futures, timeout=self.config.recursion_timeout
            )

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

        final_report = self.synthesize_findings(prompt, report, sub_reports)

        self.session_manager.update_session(
            interaction_id, "completed", result=final_report
        )

        return final_report

    def _run_recursive_child_safe(self, q, d, max_d, b, req, pid):
        agent = DeepResearchAgent(config=self.config)
        return agent._execute_recursion_level(q, d, max_d, b, req, pid)
