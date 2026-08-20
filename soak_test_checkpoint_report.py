"""Prints one row per (checkpoint, vendor) matching the "72 hour soak test"
sheet columns: Requests made / Requests failed / Failure rate / Average
response time (sec) / What went wrong.

Tab-separated output, grouped by vendor (TikTok / Instagram / YouTube) --
paste each vendor's block directly into its own sheet/tab.

Run anytime: python soak_test_checkpoint_report.py
"""

import json
from pathlib import Path

LOG_PATH = Path(__file__).parent / "data" / "soak_test" / "log.jsonl"
PLATFORM_KEYS = ["tiktok", "instagram", "youtube"]


def collect_attempts(platform_record, platform_key):
    """All actor-call attempts (including retries) for this vendor in this
    checkpoint -- for Instagram that's search + profile enrichment combined."""
    if platform_key == "instagram":
        stages = [platform_record.get("search")]
        if platform_record.get("profile_enrichment"):
            stages.append(platform_record["profile_enrichment"])
    else:
        stages = [platform_record]

    attempts = []
    for stage in stages:
        if stage:
            attempts.extend(stage.get("attempts", []))
    return attempts


def what_went_wrong(platform_record, attempts):
    if not attempts:
        return "No data"

    issues = []
    if any(a["timed_out"] for a in attempts):
        issues.append("timed out")
    if any(a["blocked_signal"] for a in attempts):
        matches = sorted({m for a in attempts for m in a["blocked_log_matches"]})
        issues.append(f"blocking signal ({', '.join(matches)})")
    if platform_record.get("zero_results"):
        issues.append("zero results")
    if platform_record.get("status") != "SUCCESS":
        issues.append("run failed")

    if not issues:
        return "Clean run"
    return "; ".join(issues)


def checkpoint_row(record, platform_key):
    platform_record = record.get("platforms", {}).get(platform_key)
    if not platform_record:
        return None

    attempts = collect_attempts(platform_record, platform_key)
    requests_made = len(attempts)
    requests_failed = sum(
        1 for a in attempts if not (a["status"] == "SUCCEEDED" and a["records_returned"] > 0)
    )
    failure_rate = round(100 * requests_failed / requests_made, 1) if requests_made else 0.0
    avg_response = (
        round(sum(a["total_seconds"] for a in attempts) / requests_made, 2) if requests_made else 0.0
    )

    return {
        "checkpoint_time": record.get("run_timestamp"),
        "requests_made": requests_made,
        "requests_failed": requests_failed,
        "failure_rate_percent": failure_rate,
        "avg_response_seconds": avg_response,
        "what_went_wrong": what_went_wrong(platform_record, attempts),
    }


def load_records():
    if not LOG_PATH.exists():
        return []
    with open(LOG_PATH, encoding="utf-8") as f:
        records = [json.loads(line) for line in f if line.strip()]
    return [r for r in records if r.get("status") != "SKIPPED_BUDGET_CAP"]


def main():
    records = load_records()
    if not records:
        print("No soak test checkpoints logged yet.")
        return

    for platform_key in PLATFORM_KEYS:
        print(f"=== {platform_key.upper()} ===")
        print("Checkpoint\tRequests made\tRequests failed\tFailure rate\tAverage response time (sec)\tWhat went wrong")
        for record in records:
            row = checkpoint_row(record, platform_key)
            if row is None:
                continue
            print(
                f"{row['checkpoint_time']}\t{row['requests_made']}\t{row['requests_failed']}\t"
                f"{row['failure_rate_percent']}%\t{row['avg_response_seconds']}\t{row['what_went_wrong']}"
            )
        print()


if __name__ == "__main__":
    main()
