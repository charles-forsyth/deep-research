# Gemini Deep Research CLI

## Project Overview
This project is a Command Line Interface (CLI) tool designed to leverage the **Gemini 3 Pro** model for "Deep Research" tasks. It enables users to conduct autonomous, multi-step research sessions directly from the terminal. Key capabilities include streaming "thinking" processes, integrating local documents via Google's File Search API, and handling follow-up questions for iterative refinement.

The tool handles the complexity of managing interaction sessions, reconnecting dropped streams, and formatting output.

## Technical Architecture
- **Language:** Python 3.12+
- **Dependency Manager:** `uv`
- **Core Libraries:**
    - `google-genai`: Official SDK for Gemini API interactions.
    - `pydantic`: For robust data validation of requests and configurations.
    - `python-dotenv`: For environment variable management.
- **Entry Point:** `deep_research.py`

## Setup & Configuration

### Prerequisites
- Python 3.12 or higher.
- `uv` installed for dependency management.
- A Google Cloud Project with the Gemini API enabled and an API Key.

### Installation
1.  **Sync Dependencies:**
    ```bash
    uv sync
    ```

2.  **Environment Configuration:**
    Set your `GEMINI_API_KEY` in a `.env` file in the project root or in `~/.config/deepresearch/.env`:
    ```env
    GEMINI_API_KEY=your_api_key_here
    ```

## Usage

The tool is executed via the `deep_research.py` script. It automatically detects and switches to the managed virtual environment.

### 1. Research Command
Initiates a new research task.

```bash
# Basic research with streaming
./deep_research.py research "Your research prompt" --stream

# Research with file uploads (creates a temporary File Search Store)
./deep_research.py research "Analyze these docs" --upload ./documents/ --stream

# Research with specific output formatting
./deep_research.py research "Compare X and Y" --format "Markdown table" --stream
```

**Key Arguments:**
- `--stream`: Enables real-time output of the model's thinking process and content.
- `--upload <paths>`: Uploads files or directories for the model to analyze.
- `--format <string>`: Appends formatting instructions to the prompt.

### 2. Follow-up Command
Asks a question about a previous session using its Interaction ID (displayed at the start of a research session).

```bash
./deep_research.py followup <INTERACTION_ID> "Can you expand on the second point?"
```

## Development & Testing

### Running Tests
The project uses `pytest` for testing.
```bash
uv run pytest
```

### Code Style
- **Type Hints:** The codebase uses Python type hints throughout.
- **Validation:** Pydantic models (`ResearchRequest`, `FollowUpRequest`) are used to validate all inputs.
