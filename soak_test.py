"""3-day Apify supplier-reliability soak test — one instrumented monitoring cycle.

Separate from app.py on purpose: app.py stays the clean V0 product harness
(see CLAUDE.md "Out of scope"). This script is a monitoring layer that
imports app.py's Actor IDs, constants, and normalize_*_item() functions but
implements its own retry loop so it can capture soak-test-specific telemetry
(queue vs execution time split, blocking-signal log scanning, first-attempt
vs recovered-after-retry) without adding that instrumentation to the V0 CLI.

Run manually (smoke test / GitHub Actions workflow_dispatch):
    python soak_test.py

Scheduled (GitHub Actions cron, every 20 min): same command, unmodified.
Each cycle requests 1 result/platform, checks the account's monthly Apify
spend against a soak-test budget cap before doing any Actor calls, and
appends one JSON line to data/soak_test/log.jsonl.
"""

import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

from app import (
    ACTOR_TIMEOUT_SECONDS,
    IG_PROFILE_ACTOR,
    IG_SEARCH_ACTOR,
    MAX_ATTEMPTS,
    RETRY_WAIT_SCHEDULE,
    TERMINAL_RUN_STATUSES,
    TIKTOK_ACTOR,
    YOUTUBE_ACTOR,
    ApifyClient,
    InvalidRequestError,
    get_first,
    normalize_instagram_item,
    normalize_tiktok_item,
    normalize_youtube_item,
    now_iso,
)

NICHE = "AI automation"
LIMIT = 1

SOAK_DIR = Path(__file__).parent / "data" / "soak_test"
LOG_PATH = SOAK_DIR / "log.jsonl"
STATE_PATH = SOAK_DIR / "budget_state.json"

# Hard cap on $ spent BY THIS SOAK TEST (measured against a baseline snapshot
# taken on the soak test's first run), independent of the account's overall
# $5/month free-tier limit. If real per-run cost tracks what we saw in manual
# testing (~$0.05-0.22/run for a full 4-actor pass) rather than the
# theoretical per-event floor (~$0.014/run), this cap will be hit well before
# 216 scheduled runs / 3 days elapse — that's intentional: protecting the
# budget takes priority over completing the full 3-day schedule.
BUDGET_CAP_USD = 3.50

BLOCK_PATTERNS = [
    "429",
    "403",
    "captcha",
    "recaptcha",
    "blocked",
    "proxy retry",
    "rate limit",
    "too many requests",
    "access denied",
    "forbidden",
]

# (schema field name, normalized-record section, key within that section)
REQUIRED_FIELD_CHECKS = [
    ("video_id", "video", "id"),
    ("video_url", "video", "url"),
    ("views", "video", "view_count"),
    ("likes", "video", "like_count"),
    ("comments", "video", "comment_count"),
    ("timestamp", "video", "upload_timestamp"),
    ("duration", "video", "duration_seconds"),
    ("thumbnail", "video", "thumbnail_url"),
    ("creator_handle", "creator", "handle"),
    ("creator_id", "creator", "id"),
    ("follower_count", "creator", "follower_count"),
    ("profile_url", "creator", "profile_url"),
]


# ---------------------------------------------------------------------------
# Instrumentation helpers
# ---------------------------------------------------------------------------

def fetch_run_log_text(client, run_id):
    try:
        return client.run(run_id).log().get() or ""
    except Exception:  # noqa: BLE001 - log fetch is best-effort telemetry, never fatal
        return ""


def detect_block_signals(log_text):
    lowered = log_text.lower()
    matches = sorted({pattern for pattern in BLOCK_PATTERNS if pattern in lowered})
    return {
        "blocked_signal": bool(matches),
        "http_403_signal": "403" in matches,
        "http_429_signal": "429" in matches,
        "blocked_log_matches": matches,
    }


def check_required_fields(normalized_record):
    if normalized_record is None:
        return {name: False for name, _, _ in REQUIRED_FIELD_CHECKS}
    return {
        name: normalized_record.get(section, {}).get(key) is not None
        for name, section, key in REQUIRED_FIELD_CHECKS
    }


def run_actor_monitored(client, actor_id, run_input):
    """Run actor_id up to MAX_ATTEMPTS times, bounded by ACTOR_TIMEOUT_SECONDS
    per attempt (same constants as app.py's harness), recording queue vs
    execution time and blocking signals for every attempt.

    Returns (items, attempts).
    """
    attempts = []

    for attempt_number in range(1, MAX_ATTEMPTS + 1):
        started_at = now_iso()
        wall_start = time.monotonic()
        run_id = None
        dataset_id = None
        items = []
        status = "ERROR"
        error_message = None
        execution_seconds = None
        log_text = ""

        try:
            run = client.actor(actor_id).start(run_input=run_input)
            run_id = run.id
            run_client = client.run(run_id)
            finished = run_client.wait_for_finish(wait_duration=timedelta(seconds=ACTOR_TIMEOUT_SECONDS))

            if finished is None or finished.status not in TERMINAL_RUN_STATUSES:
                status = "TIMED_OUT"
                try:
                    run_client.abort()
                except Exception:  # noqa: BLE001 - best-effort cleanup of a stuck run
                    pass
            else:
                status = finished.status
                dataset_id = finished.default_dataset_id
                if finished.stats is not None:
                    execution_seconds = getattr(finished.stats, "run_time_secs", None)
                if status == "SUCCEEDED":
                    items = client.dataset(dataset_id).list_items().items

            if run_id:
                log_text = fetch_run_log_text(client, run_id)
        except InvalidRequestError as exc:
            status = "CONFIG_ERROR"
            error_message = str(exc)
        except Exception as exc:  # noqa: BLE001 - recorded on the attempt, retried below
            status = "ERROR"
            error_message = str(exc)

        total_seconds = time.monotonic() - wall_start
        queue_seconds = None
        if execution_seconds is not None:
            queue_seconds = round(max(0.0, total_seconds - execution_seconds), 2)

        block_info = detect_block_signals(log_text)

        attempt = {
            "attempt_number": attempt_number,
            "actor": actor_id,
            "run_id": run_id,
            "dataset_id": dataset_id,
            "status": status,
            "started_at": started_at,
            "finished_at": now_iso(),
            "queue_seconds": queue_seconds,
            "execution_seconds": round(execution_seconds, 2) if execution_seconds is not None else None,
            "total_seconds": round(total_seconds, 2),
            "records_returned": len(items),
            "zero_results": status == "SUCCEEDED" and len(items) == 0,
            "timed_out": status == "TIMED_OUT",
            "error": error_message,
            **block_info,
        }
        attempts.append(attempt)

        if status == "CONFIG_ERROR":
            return [], attempts
        if status == "SUCCEEDED" and items:
            return items, attempts
        if attempt_number < MAX_ATTEMPTS:
            time.sleep(RETRY_WAIT_SCHEDULE[attempt_number])

    return [], attempts


def attempt_outcome_label(attempt):
    """SUCCESS only if the attempt actually returned data — a SUCCEEDED Apify
    status with 0 records is labeled ZERO_RESULTS, not SUCCESS, so it never
    reads as a pass next to recovered_after_retry=true."""
    if attempt["status"] == "SUCCEEDED":
        return "SUCCESS" if attempt["records_returned"] > 0 else "ZERO_RESULTS"
    return attempt["status"]


def summarize_attempts(attempts):
    """Roll a list of per-attempt records into the first-attempt vs
    final/recovered-after-retry summary the soak test is built to answer."""
    if not attempts:
        return None

    first = attempts[0]
    last = attempts[-1]
    first_passed = first["status"] == "SUCCEEDED" and first["records_returned"] > 0
    final_passed = last["status"] == "SUCCEEDED" and last["records_returned"] > 0

    return {
        "run_id": last["run_id"],
        "queue_seconds": last["queue_seconds"],
        "execution_seconds": last["execution_seconds"],
        "total_seconds": round(sum(a["total_seconds"] for a in attempts), 2),
        "attempts_used": len(attempts),
        "first_attempt": attempt_outcome_label(first),
        "final_status": "SUCCESS" if final_passed else last["status"],
        "recovered_after_retry": (not first_passed) and final_passed,
        "zero_results": last["zero_results"],
        "timed_out": any(a["timed_out"] for a in attempts),
        "blocked_signal": any(a["blocked_signal"] for a in attempts),
        "http_403_signal": any(a["http_403_signal"] for a in attempts),
        "http_429_signal": any(a["http_429_signal"] for a in attempts),
        "blocked_log_matches": sorted({m for a in attempts for m in a["blocked_log_matches"]}),
        "attempts": attempts,
    }


# ---------------------------------------------------------------------------
# Per-platform monitors
# ---------------------------------------------------------------------------

def monitor_tiktok(client):
    run_input = {
        "searchQueries": [NICHE],
        "searchSection": "/video",
        "resultsPerPage": LIMIT,
        "scrapeRelatedSearchWords": False,
    }
    items, attempts = run_actor_monitored(client, TIKTOK_ACTOR, run_input)
    summary = summarize_attempts(attempts)
    normalized = normalize_tiktok_item(items[0], NICHE, now_iso()) if items else None

    return {
        "status": "SUCCESS" if items else "FAILED",
        "requested": LIMIT,
        "returned": len(items),
        **summary,
        "fields": check_required_fields(normalized),
    }


def monitor_youtube(client):
    run_input = {
        "searchQueries": [NICHE],
        "maxResults": 0,
        "maxResultsShorts": LIMIT,
        "maxResultStreams": 0,
    }
    items, attempts = run_actor_monitored(client, YOUTUBE_ACTOR, run_input)
    summary = summarize_attempts(attempts)
    normalized = normalize_youtube_item(items[0], NICHE, now_iso()) if items else None

    return {
        "status": "SUCCESS" if items else "FAILED",
        "requested": LIMIT,
        "returned": len(items),
        **summary,
        "fields": check_required_fields(normalized),
    }


def monitor_instagram(client):
    search_input = {
        "search": NICHE,
        "searchType": "popular",
        "searchLimit": LIMIT,
        "enhanceUserSearchWithFacebookPage": False,
    }
    reel_items, search_attempts = run_actor_monitored(client, IG_SEARCH_ACTOR, search_input)
    search_summary = summarize_attempts(search_attempts)

    record = {
        "status": "FAILED",
        "requested": LIMIT,
        "returned": len(reel_items),
        "search": search_summary,
        "profile_enrichment": None,
        "zero_results": search_summary["zero_results"],
        "timed_out": search_summary["timed_out"],
        "blocked_signal": search_summary["blocked_signal"],
        "http_403_signal": search_summary["http_403_signal"],
        "http_429_signal": search_summary["http_429_signal"],
        "total_seconds": search_summary["total_seconds"],
        "fields": check_required_fields(None),
    }

    if not reel_items:
        return record

    usernames = sorted(
        {
            get_first(item, ["ownerUsername", "username", "ownerName"])
            for item in reel_items
            if get_first(item, ["ownerUsername", "username", "ownerName"])
        }
    )

    profile_items, profile_summary = [], None
    if usernames:
        profile_items, profile_attempts = run_actor_monitored(client, IG_PROFILE_ACTOR, {"usernames": usernames})
        profile_summary = summarize_attempts(profile_attempts)

    profile_map = {}
    for profile in profile_items:
        uname = get_first(profile, ["username", "handle"])
        if uname:
            profile_map[uname] = profile

    normalized = normalize_instagram_item(reel_items[0], profile_map, NICHE, now_iso())

    record.update(
        {
            "status": "SUCCESS",
            "profile_enrichment": profile_summary,
            "fields": check_required_fields(normalized),
        }
    )
    if profile_summary:
        record["blocked_signal"] = record["blocked_signal"] or profile_summary["blocked_signal"]
        record["http_403_signal"] = record["http_403_signal"] or profile_summary["http_403_signal"]
        record["http_429_signal"] = record["http_429_signal"] or profile_summary["http_429_signal"]
        record["total_seconds"] = round(record["total_seconds"] + profile_summary["total_seconds"], 2)

    return record


# ---------------------------------------------------------------------------
# Budget gate + logging
# ---------------------------------------------------------------------------

def load_budget_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return None


def save_budget_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def current_monthly_spend(client):
    usage = client.user().monthly_usage()
    return float(usage.total_usage_credits_usd_after_volume_discount)


def append_log(record):
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def main():
    load_dotenv()
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        print("Error: APIFY_TOKEN not set.")
        sys.exit(1)

    client = ApifyClient(token)
    run_timestamp = now_iso()

    current_spend = current_monthly_spend(client)
    state = load_budget_state()
    if state is None:
        state = {"baseline_usd": current_spend, "started_at": run_timestamp}
        save_budget_state(state)
        print(f"Soak test baseline captured: ${current_spend:.4f} (account monthly spend at start)")

    spent_by_soak_test = round(current_spend - state["baseline_usd"], 4)

    if spent_by_soak_test >= BUDGET_CAP_USD:
        record = {
            "run_timestamp": run_timestamp,
            "status": "SKIPPED_BUDGET_CAP",
            "spent_by_soak_test_usd": spent_by_soak_test,
            "budget_cap_usd": BUDGET_CAP_USD,
        }
        append_log(record)
        print(f"Budget cap reached: ${spent_by_soak_test:.4f} >= ${BUDGET_CAP_USD} cap. Skipping this cycle (no Actor calls made).")
        return

    wall_start = time.monotonic()
    platforms = {
        "tiktok": monitor_tiktok(client),
        "instagram": monitor_instagram(client),
        "youtube": monitor_youtube(client),
    }
    run_total_seconds = round(time.monotonic() - wall_start, 2)

    record = {
        "run_timestamp": run_timestamp,
        "niche": NICHE,
        "requested_per_platform": LIMIT,
        "spent_by_soak_test_usd": spent_by_soak_test,
        "platforms": platforms,
        "run_total_seconds": run_total_seconds,
    }
    append_log(record)

    for name, result in platforms.items():
        print(f"{name:<10} {result['status']:<8} returned={result['returned']} total={result['total_seconds']:.1f}s")
    print(f"Run total: {run_total_seconds:.1f}s | Soak-test spend so far: ${spent_by_soak_test:.4f} / ${BUDGET_CAP_USD}")


if __name__ == "__main__":
    main()
