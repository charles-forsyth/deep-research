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

### 4. Recursive Deep Research (New!)
For complex topics, use `--depth 2` to enable the autonomous recursive mode.
1.  **Phase 1:** The agent performs initial research and generates a report.
2.  **Gap Analysis:** It autonomously analyzes the report for missing information or "gaps".
3.  **Deep Dive:** It spawns parallel child research tasks to investigate these gaps.
4.  **Synthesis:** It merges all findings into a final, comprehensive answer.

```bash
deep-research research "Deep analysis of quantum computing timelines" --depth 2
```

### 5. Headless Research (Fire & Forget)
For long tasks, start the research in the background and check back later.

```bash
# Start a detached session
deep-research start "Detailed analysis of quantum computing trends"

# Check status
deep-research list

# View result when done
deep-research show 1
```

### 6. Follow-up Questions
Interact with your research history. You can use the local **Session ID** (e.g., `1`) or the API Interaction ID.

```bash
# Resume session #1
deep-research followup 1 "Can you explain the error correction part simply?"
```

### 7. Manage History & Maintenance
Review your past research sessions and keep your system clean.

```bash
# List recent sessions
deep-research list

# Show the report from a specific session ID
deep-research show 1 --save report.html

# Delete a session
deep-research delete 1

# Garbage Collection (Cleanup Stale Cloud Files)
deep-research cleanup
```

## 💡 Use Case Gallery

Unlock the full potential of your autonomous research agent with these powerful workflows.

### 🛠️ Developer & Technical

#### 1. The "Codebase Archaeologist"
Inherit a messy legacy project? Use this to understand it fast.
*   **The Power:** Uses `--upload` to ingest the file structure and key files, and `--depth 2` to recursively analyze subsystems and identify architectural patterns.
*   **Command:**
    ```bash
    deep-research "Analyze this codebase. Identify the tech stack, key patterns, and security risks." --upload ./src/ --depth 2
    ```

#### 2. Technical Troubleshooting
Stuck on an obscure error? Let the agent find the fix.
*   **The Power:** Synthesizes solutions from StackOverflow, GitHub Issues, and official documentation into a single, verified fix, saving you hours of tab-switching.
*   **Command:**
    ```bash
    deep-research "Fix 'Error X' in System Y. Synthesize solutions from GitHub issues and docs."
    ```

#### 3. Content Repurposing
Need to turn a whitepaper into a tweet thread?
*   **The Power:** Uploads long PDF reports and intelligently reformats the key insights into specific social media formats.
*   **Command:**
    ```bash
    deep-research "Turn this report into a Tweet thread and a LinkedIn post." --upload report.pdf
    ```

### 💼 Business & Strategy

#### 4. The "Competitor Matrix"
Need to compare products for a strategy meeting?
*   **The Power:** Uses `--breadth 5` to spawn parallel agents that research multiple competitors simultaneously, and `--format CSV` to generate a spreadsheet ready for Excel.
*   **Command:**
    ```bash
    deep-research "Compare top 5 CRM tools. Columns: Pricing, Features, Sentiment." --breadth 5 --format CSV --output crm.csv
    ```

#### 5. Supply Chain Risk Assessment
Worried about logistics?
*   **The Power:** Traces product dependencies recursively to identify geopolitical bottlenecks (e.g., rare earth metals) and single points of failure.
*   **Command:**
    ```bash
    deep-research "Trace the supply chain of Lithium batteries. Identify geopolitical risks." --depth 2
    ```

#### 6. Investment Due Diligence
Considering an investment or partnership?
*   **The Power:** Recursively investigates financial health (10-K), recent lawsuits, and leadership history to create a comprehensive risk profile.
*   **Command:**
    ```bash
    deep-research "Deep dive on [Company]. Focus on financial health, lawsuits, and leadership." --depth 2
    ```

#### 7. Grant Proposal Generator
Need funding?
*   **The Power:** Researches specific funding agencies (NSF, NIH) and tailors your project description to align perfectly with their current strategic goals.
*   **Command:**
    ```bash
    deep-research "Draft a grant proposal for [Project Idea] aligned with NSF strategic goals."
    ```

### 🎓 Academic & Legal

#### 8. The "Academic Literature Review"
Writing a thesis or paper?
*   **The Power:** The "Gap Analysis" feature. The agent creates an initial review, realizes "I found papers on X, but I'm missing Y," and automatically spawns child tasks to find the missing citations.
*   **Command:**
    ```bash
    deep-research start "Write a literature review on microplastics in soil. Identify research gaps." --depth 3 --breadth 4
    ```

#### 9. Legal Precedent Search
Need case law?
*   **The Power:** Recursively analyzes legal databases to find relevant case law in specific jurisdictions and highlights contradictory rulings.
*   **Command:**
    ```bash
    deep-research "Find case law regarding [Legal Concept] in [Jurisdiction]. Analyze contradictions."
    ```

#### 10. Historical "What If" Analysis
Studying history?
*   **The Power:** Synthesizes views from multiple historians to construct a detailed counter-factual analysis of major historical events.
*   **Command:**
    ```bash
    deep-research "Analyze the Battle of Midway outcomes if [Event X] had changed."
    ```

### 🌍 Life & Automation

#### 11. The "Daily Briefing" Pipeline
Want a custom news feed?
*   **The Power:** The `-q` (Quiet Mode) allows you to run this in a cron job or script, piping the output directly to email or Slack without any logs.
*   **Command:**
    ```bash
    deep-research -q "Summarize global AI news from the last 24 hours." > morning_brief.txt
    ```

#### 12. Travel Itinerary Planner
Planning a complex trip?
*   **The Power:** Depth ensures specific constraints (e.g., "Gluten-Free", "Kid-Friendly") are checked against actual restaurant menus and venue policies, not just generic lists.
*   **Command:**
    ```bash
    deep-research "2-week Japan trip. Gluten-free food, kid-friendly hiking, anime spots." --depth 2
    ```

#### 13. The "Deep Dive Interview Prep"
Preparing for a big interview?
*   **The Power:** Recursively looks at the company's recent challenges, the CEO's speeches, and employee reviews to give you talking points that impress.
*   **Command:**
    ```bash
    deep-research "Deep dive on [Company]. Focus on recent product launches and culture." --depth 2
    ```

#### 14. The "Gift Wizard"
Need a gift for a hobby you don't understand?
*   **The Power:** Checks Reddit threads and niche forums to find durable, highly-rated items that enthusiasts actually respect, within your budget.
*   **Command:**
    ```bash
    deep-research "Best gift for a 30yo rock climber who loves coffee. Budget $200."
    ```

#### 15. Fact-Checking / Debunking
Is that viral video true?
*   **The Power:** Traces claims back to primary sources (studies, raw footage) to identify logical fallacies and misinformation.
*   **Command:**
    ```bash
    deep-research "Verify the claims in [Viral Article]. Trace citations to primary sources."
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
