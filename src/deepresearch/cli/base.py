from pydantic import BaseModel


class ResearchRequest(BaseModel):
    prompt: str
    stores: list[str] | None = None
    stream: bool = False
    output_format: str | None = None
    upload_paths: list[str] | None = None
    output_file: str | None = None
    adopt_session_id: int | None = None
    depth: int = 1
    breadth: int = 3  # Max child tasks per node

    @property
    def final_prompt(self) -> str:
        base = self.prompt
        if self.output_format:
            base += f"\n\nFormat the output as follows: {self.output_format}"

        # Auto-append structural instructions based on filename extension
        if self.output_file:
            if self.output_file.lower().endswith(".json"):
                base += "\n\nIMPORTANT: Output the final report as valid JSON inside a ```json code block."
            elif self.output_file.lower().endswith(".csv"):
                base += "\n\nIMPORTANT: Output the final report as valid CSV inside a ```csv code block."

        return base

    @property
    def tools_config(self) -> list[dict] | None:
        if self.stores:
            return [{"type": "file_search", "file_search_store_names": self.stores}]
        return None


class FollowUpRequest(BaseModel):
    interaction_id: str
    prompt: str
