"""
LLM integration — Concept #7: one narrow AI job behind an endpoint, with
input validation and a cost log.

The job is deliberately narrow: turn a batch of raw ERROR/WARN log lines
into a short plain-English incident summary. It is NOT a general chatbot
bolted onto the app.

If no ANTHROPIC_API_KEY is configured (e.g. a stranger cloning the repo),
this falls back to a deterministic local summarizer so the demo still runs
end-to-end for free, with no credit card and no key required. If a key is
present, it calls the real API.
"""
import os
import time

from . import db

MAX_INPUT_CHARS = 6000  # validation: refuse to send unbounded input to the model
# Rough public per-token pricing used only to produce an honest order-of-magnitude
# estimate in the cost log — not a billing-accurate figure.
EST_COST_PER_1K_INPUT_CHARS = 0.0008
EST_COST_PER_1K_OUTPUT_CHARS = 0.004


def _local_fallback_summary(log_lines: list[str]) -> str:
    errors = [l for l in log_lines if "ERROR" in l or "error" in l.lower()]
    warns = [l for l in log_lines if "WARN" in l or "warn" in l.lower()]
    top = errors[0] if errors else (warns[0] if warns else "no notable events")
    return (
        f"Local summary (no ANTHROPIC_API_KEY set): {len(errors)} error-level and "
        f"{len(warns)} warn-level events in this window. Most frequent signal: '{top}'. "
        f"Set ANTHROPIC_API_KEY to get a real LLM-written narrative instead."
    )


def summarize_incidents(log_lines: list[str], service_id: int | None = None) -> dict:
    joined = "\n".join(log_lines)
    if not joined.strip():
        return {"summary": "No log lines provided in this window.", "estimated_cost_usd": 0.0}

    # --- validation ---
    if len(joined) > MAX_INPUT_CHARS:
        joined = joined[:MAX_INPUT_CHARS]  # truncate rather than send an unbounded prompt

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        summary = _local_fallback_summary(log_lines)
        output_chars = len(summary)
    else:
        import requests

        prompt = (
            "You are an SRE assistant. Summarize the following service log lines into "
            "a short, plain-English incident summary (max 4 sentences). Call out the "
            "most likely root cause if the evidence supports one.\n\nLOG LINES:\n" + joined
        )
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        summary = "".join(block.get("text", "") for block in data.get("content", []))
        output_chars = len(summary)

    input_chars = len(joined)
    est_cost = (input_chars / 1000) * EST_COST_PER_1K_INPUT_CHARS + (
        output_chars / 1000
    ) * EST_COST_PER_1K_OUTPUT_CHARS

    with db.db() as conn:
        conn.execute(
            "INSERT INTO llm_cost_log (service_id, input_chars, output_chars, estimated_cost_usd, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (service_id, input_chars, output_chars, est_cost, time.time()),
        )

    return {"summary": summary, "estimated_cost_usd": round(est_cost, 6)}
