"""Cost estimate for a research run (no API calls). Shared by the CLI and dashboard.

Per agent run, from Google's Deep Research docs ("Estimated costs", fetched 2026-10-03):
standard about 80 searches, 250k input tokens (50-70% cached), 60k output, "$1-3 per
task"; Max up to about 160 searches, 900k input, 80k output, "$3-7 per task". Gemini
3.1 Pro rates ($2/1M input, $0.20 cached, $12/1M output) and Google Search grounding at
$14 per 1,000 queries. Our own runs have used fewer searches than Google's figure
(about 26 on average), so the search line is a ceiling, not a typical bill.
"""

from __future__ import annotations

COST_INPUT_1M = 2.00
COST_CACHED_1M = 0.20
COST_OUTPUT_1M = 12.00
COST_SEARCH_1K = 14.00
CACHED_FRACTION = 0.6

PROFILES = {
    # input tokens, output tokens, search queries per agent run
    "standard": {"input": 250_000, "output": 60_000, "searches": 80},
    "max": {"input": 900_000, "output": 80_000, "searches": 160},
}


def nodes_for(depth: int, breadth: int) -> int:
    """Agent runs in a recursive run: 1 + b + b^2 ... for `depth` levels."""
    return sum(pow(breadth, d) for d in range(max(depth, 1)))


def estimate(
    depth: int, breadth: int, file_bytes: int = 0, agent: str | None = None
) -> dict:
    prof = PROFILES["max" if (agent or "").lower() == "max" else "standard"]
    nodes = nodes_for(depth, breadth)
    file_tokens = file_bytes * 0.25
    total_in = nodes * prof["input"] + nodes * file_tokens
    total_out = nodes * prof["output"]
    cached = nodes * prof["input"] * CACHED_FRACTION
    tokens_usd = (
        (total_in - cached) / 1e6 * COST_INPUT_1M
        + cached / 1e6 * COST_CACHED_1M
        + total_out / 1e6 * COST_OUTPUT_1M
    )
    searches = nodes * prof["searches"]
    search_usd = searches / 1000 * COST_SEARCH_1K
    return {
        "agent": "max" if prof is PROFILES["max"] else "standard",
        "nodes": nodes,
        "input_tokens": int(total_in),
        "output_tokens": int(total_out),
        "file_tokens": int(file_tokens),
        "searches": searches,
        "tokens_usd": round(tokens_usd, 2),
        "search_usd": round(search_usd, 2),
        # tokens are the dependable part; searches are Google's upper figure
        "cost_usd": round(tokens_usd, 2),
        "cost_high_usd": round(tokens_usd + search_usd, 2),
    }
