# Gemini Deep Research Agent

A command-line interface (CLI) for the Gemini Deep Research Agent, powered by Google's GenAI. This tool allows you to conduct deep, multi-step research tasks, optionally using your own documents, and receive comprehensive reports.

## Features

*   **Deep Research:** Autonomously plans and executes multi-step research using web search.
*   **Streaming Support:** Receive real-time updates and "thinking summaries" as the agent works.
*   **Auto-Resume:** Robustly handles network interruptions by automatically reconnecting to the stream.
*   **File Search:** (Optional) Integrate with your own data stores (`fileSearchStores`).
*   **Follow-up Questions:** Ask clarifying questions about the generated report.
*   **Portable Execution:** Automatically switches to the project's virtual environment for seamless execution.

## Prerequisites

*   Python 3.12+
*   [uv](https://github.com/astral-sh/uv) (for dependency management)
*   A Google Cloud Project with the Gemini API enabled.
*   A valid API Key.

## Installation

1.  **Clone the repository:**
    ```bash
    git clone git@github.com:charles-forsyth/deep-research.git
    cd deep-research
    ```

2.  **Install dependencies:**
    This project uses `uv` to manage dependencies.
    ```bash
    uv sync
    ```

### 3. Configure Environment

You can configure the API key in one of two ways:

**Option A: Global Configuration (Recommended)**
Create a config directory and add your key. This allows you to run the tool from anywhere.
```bash
mkdir -p ~/.config/deepresearch
echo "GEMINI_API_KEY=your_api_key_here" > ~/.config/deepresearch/.env
```

**Option B: Local Configuration**
Create a `.env` file in the directory where you run the script.
```bash
echo "GEMINI_API_KEY=your_api_key_here" > .env
```

## Usage

You can run the script directly. It will automatically detect if it needs to switch to the project's virtual environment.

### 1. Conduct Research

**Basic Stream (Recommended):**
```bash
./deep_research.py research "Research the history of Google TPUs" --stream
```

**Polling Mode (Background):**
```bash
./deep_research.py research "Research the competitive landscape of EV batteries"
```

**With Formatting Instructions:**
```bash
./deep_research.py research "Compare Go and Rust" --stream --format "Technical Report with a table"
```

**With File Search (User Data):**
```bash
./deep_research.py research "Analyze Q3 financial results" --stores "projects/123/locations/us-central1/collections/default/dataStores/my-store" --stream
```

### 2. Follow-up Questions

After a research task completes, you will get an `Interaction ID` (e.g., `v1_abc123...`). You can use this to ask follow-up questions.

```bash
./deep_research.py followup <INTERACTION_ID> "Can you elaborate on the second point?"
```

## Development

*   **Run Tests:**
    ```bash
    uv run pytest
    ```
    (Tests are also run automatically via a pre-commit hook).

## License

[MIT](LICENSE)
