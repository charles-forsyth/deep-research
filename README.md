# 🧠 Gemini Deep Research CLI

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Powered by Gemini](https://img.shields.io/badge/Powered%20by-Gemini%203%20Pro-4285F4.svg)](https://deepmind.google/technologies/gemini/)

A production-ready command-line interface for the **Gemini Deep Research Agent**. 

This tool unlocks the power of Google's most advanced autonomous research model, allowing you to conduct deep, multi-step investigations, analyze local documents, and generate comprehensive reports—all from your terminal.

## ✨ Key Features

*   **🚀 Autonomous Deep Research:** Powered by Gemini 3 Pro, it plans, searches, reads, and synthesizes complex topics.
*   **📂 Smart Context Ingestion:** Instantly analyze local PDFs, text files, or folders. The tool handles cloud upload/cleanup automatically.
*   **💾 Structured Data Export:** Save reports directly to JSON or CSV files with automatic schema enforcement (`--output`).
*   **👻 Headless Mode:** Fire-and-forget research tasks (`start`) that run in the background. Perfect for long-running investigations.
*   **🗄️ Session History:** Automatically saves your research history to a local database. List past tasks and retrieve old reports instantly.
*   **⚡ Real-Time Streaming:** Watch the agent's "Thought Process" in real-time as it navigates the web.
*   **🛡️ Robust & Resilient:** Auto-resumes sessions if the network drops. 
*   **💬 Interactive Follow-ups:** Chat with the finished report to ask clarifying questions.
*   **📦 Portable:** Auto-detects its environment. Install globally via `uv` or run locally.

## 🚀 Installation

The recommended way to install is using `uv` (a modern Python package manager).

### Option 1: Global Installation (Recommended)
This makes the `deep-research` command available anywhere on your system.

```bash
uv tool install git+https://github.com/charles-forsyth/deep-research.git
```

### Option 2: Developer Setup (Clone & Run)

```bash
git clone https://github.com/charles-forsyth/deep-research.git
cd deep-research
uv sync
```

## ⚙️ Configuration

You need a Google GenAI API Key.

1.  **Get a Key:** [Google AI Studio](https://aistudio.google.com/app/apikey)
2.  **Set it up:**
    *   **Global (Recommended):** Run once, use anywhere.
        ```bash
        mkdir -p ~/.config/deepresearch
        echo "GEMINI_API_KEY=your_key_here" > ~/.config/deepresearch/.env
        ```
    *   **Local:** Create a `.env` file in your current directory.

## 📖 Usage Guide

### 1. Basic Research
Conduct a deep dive into a topic. The `--stream` flag shows the agent's thinking.

```bash
deep-research research "The history of the internet" --stream
```

### 2. Research Your Own Files (RAG)
Have a contract, a paper, or a set of notes? Analyze them instantly.

```bash
# Analyze a single file
deep-research research "What are the liability terms?" --upload ./contract.pdf --stream

# Analyze a whole folder
deep-research research "Summarize the key findings" --upload ./research_papers/ --stream
```
*Note: Files are uploaded to a temporary secure store and automatically deleted after the session.*

### 3. Custom Formatting & Export
Steer the output format and save structured data.

```bash
# Save as Markdown
deep-research research "Compare GPU prices" --output report.md

# Save as JSON (Schema Enforced)
deep-research research "List top 5 cloud providers with market share" --output market_data.json

# Save as CSV
deep-research research "Table of US Presidents and their terms" --output presidents.csv
```

### 4. Headless Research (Fire & Forget)
For long tasks, start the research in the background and check back later.

```bash
# Start a detached session
deep-research start "Detailed analysis of quantum computing trends"

# Check status
deep-research list

# View result when done
deep-research show 1
```

### 5. Follow-up Questions
Interact with your research history. You can use the local **Session ID** (e.g., `1`) or the API Interaction ID.

```bash
# Resume session #1
deep-research followup 1 "Can you explain the error correction part simply?"
```

### 6. Manage History
Review your past research sessions.

```bash
# List recent sessions
deep-research list

# Show the report from a specific session ID
deep-research show 1
```

## 🛠️ Development

We welcome contributions! This project uses `uv` for dependency management and `pytest` for testing.

```bash
# Run tests
uv run pytest

# Install pre-commit hooks
git config core.hooksPath .git/hooks
```

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.