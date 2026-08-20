"""Aggregates data/soak_test/log.jsonl into a supplier-reliability summary.

Run anytime during or after the soak test:
    python soak_test_report.py
"""

import json
import statistics
from pathlib import Path

LOG_PATH = Path(__file__).parent / "data" / "soak_test" / "log.jsonl"

PLATFORM_KEYS = ["tiktok", "instagram", "youtube"]


def load_records():
    if not LOG_PATH.exists():
        return []
    records = []
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return [r for r in records if r.get("status") != "SKIPPED_BUDGET_CAP"]


def percentile(values, pct):
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * (pct / 100)
    f, c = int(k), min(int(k) + 1, len(values) - 1)
    if f == c:
        return round(values[f], 2)
    return round(values[f] + (values[c] - values[f]) * (k - f), 2)


def summarize_platform(records, platform_key):
    tests = successes = zero_results = timeouts = blocked = recovered = 0
    first_attempt_successes = 0
    seconds = []

    for record in records:
        platform = record.get("platforms", {}).get(platform_key)
        if not platform:
            continue
        tests += 1
        if platform.get("status") == "SUCCESS":
            successes += 1
        if platform.get("zero_results"):
            zero_results += 1
        if platform.get("timed_out"):
            timeouts += 1
        if platform.get("blocked_signal"):
            blocked += 1

        # For TikTok/YouTube, first-attempt data lives at the top level
        # (summarize_attempts() spread into the platform dict); for
        # Instagram it's nested under "search" (the discovery stage).
        stage = platform.get("search") if platform_key == "instagram" else platform
        if stage:
            if stage.get("first_attempt") == "SUCCESS":
                first_attempt_successes += 1
            if stage.get("recovered_after_retry"):
                recovered += 1

        total_seconds = platform.get("total_seconds")
        if total_seconds is not None:
            seconds.append(total_seconds)

    failures = tests - successes
    return {
        "tests": tests,
        "successes": successes,
        "failures": failures,
        "failure_rate_percent": round(100 * failures / tests, 2) if tests else None,
        "zero_result_rate_percent": round(100 * zero_results / tests, 2) if tests else None,
        "timeout_rate_percent": round(100 * timeouts / tests, 2) if tests else None,
        "blocked_runs": blocked,
        "first_attempt_success_rate_percent": round(100 * first_attempt_successes / tests, 2) if tests else None,
        "recovered_after_retry_count": recovered,
        "median_seconds": round(statistics.median(seconds), 2) if seconds else None,
        "p95_seconds": percentile(seconds, 95),
        "max_seconds": round(max(seconds), 2) if seconds else None,
    }


def main():
    records = load_records()
    skipped = 0
    if LOG_PATH.exists():
        with open(LOG_PATH, encoding="utf-8") as f:
            skipped = sum(1 for line in f if line.strip() and json.loads(line).get("status") == "SKIPPED_BUDGET_CAP")

    summary = {
        "completed_runs": len(records),
        "budget_capped_skips": skipped,
        "niche": records[0]["niche"] if records else None,
    }
    for key in PLATFORM_KEYS:
        summary[key] = summarize_platform(records, key)

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
