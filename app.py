"""Apify Short-Video Discovery V0 — terminal probe for TikTok, Instagram Reels, and YouTube Shorts."""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

try:
    from apify_client import ApifyClient
    from apify_client.errors import InvalidRequestError
except ImportError:
    print("Missing dependency 'apify-client'. Run: pip install -r requirements.txt")
    sys.exit(1)

TIKTOK_ACTOR = "clockworks/tiktok-scraper"
IG_SEARCH_ACTOR = "apify/instagram-search-scraper"
IG_PROFILE_ACTOR = "apify/instagram-profile-scraper"
YOUTUBE_ACTOR = "streamers/youtube-scraper"

# An individual Actor attempt must never block the app forever. We start the
# run asynchronously and long-poll RunClient.wait_for_finish() bounded by
# this many seconds; if the run hasn't reached a terminal status by then, we
# abort it through the Apify API rather than waiting longer.
ACTOR_TIMEOUT_SECONDS = 120

# TikTok/Instagram runs are flaky: they can fail, abort, time out, or
# "succeed" with zero records. Retry up to this many attempts, waiting
# progressively longer between attempts (10s, then 20s).
MAX_ATTEMPTS = 3
RETRY_WAIT_SCHEDULE = {1: 10, 2: 20}  # wait (sec) after attempt N fails, before attempt N+1

TERMINAL_RUN_STATUSES = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}

DATA_DIR = Path(__file__).parent / "data"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(text):
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower()).strip("_")
    return slug or "niche"


def stringify(value):
    return None if value is None else str(value)


def get_first(d, keys):
    for key in keys:
        value = d.get(key)
        if value is not None:
            return value
    return None


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


def parse_duration_to_seconds(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        parts = value.strip().split(":")
        try:
            parts = [int(p) for p in parts]
        except ValueError:
            return None
        seconds = 0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds
    return None


def describe_attempt_failure(attempt):
    """Human-readable reason an attempt didn't count as a pass, for errors.json / terminal output."""
    if attempt["error"]:
        return attempt["error"]
    status = attempt["status"]
    if status == "TIMED_OUT":
        return f"Actor exceeded configured maximum wait ({ACTOR_TIMEOUT_SECONDS} sec)"
    if status == "SUCCEEDED":
        return "Run completed but returned 0 records"
    if status == "CONFIG_ERROR":
        return "Actor rejected the run input (schema/validation error)"
    return f"Actor run ended with status {status}"


def run_actor_with_retries(client, platform, actor_id, run_input, print_fn):
    """Run `actor_id` up to MAX_ATTEMPTS times, each attempt bounded by ACTOR_TIMEOUT_SECONDS.

    Starts the run asynchronously (ActorClient.start), then long-polls
    RunClient.wait_for_finish(wait_duration=...) up to our timeout. If the
    run hasn't reached a terminal status by then, it is aborted via
    RunClient.abort() and the attempt is recorded as TIMED_OUT. A run is
    retried on failure/abort/timeout, or on SUCCEEDED-with-zero-records.
    A 400 InvalidRequestError (bad Actor input) fails immediately without
    retrying, since retrying an invalid input three times can't help.

    Returns (items, attempts, status, retry_wait_seconds) where status is
    one of "PASS", "FAILED", "NO_RESULTS", "CONFIG_ERROR".
    """
    attempts = []
    retry_wait_seconds = 0.0

    for attempt_number in range(1, MAX_ATTEMPTS + 1):
        print_fn(f"Attempt {attempt_number}/{MAX_ATTEMPTS}")

        started_at = now_iso()
        start = time.monotonic()
        run_id = None
        dataset_id = None
        items = []
        status = "ERROR"
        error_message = None

        try:
            run = client.actor(actor_id).start(run_input=run_input)
            run_id = run.id
            print_fn("Actor started")
            print_fn(f"Run ID: {run_id}")
            print_fn(f"Timeout: {ACTOR_TIMEOUT_SECONDS} sec")

            run_client = client.run(run_id)
            finished = run_client.wait_for_finish(wait_duration=timedelta(seconds=ACTOR_TIMEOUT_SECONDS))

            if finished is None or finished.status not in TERMINAL_RUN_STATUSES:
                status = "TIMED_OUT"
                elapsed = time.monotonic() - start
                print_fn(f"Attempt {attempt_number} timed out after {elapsed:.1f} sec")
                print_fn("Aborting run...")
                try:
                    run_client.abort()
                except Exception:  # noqa: BLE001 - best-effort cleanup of a stuck run
                    pass
            else:
                status = finished.status
                dataset_id = finished.default_dataset_id
                if status == "SUCCEEDED":
                    items = client.dataset(dataset_id).list_items().items
        except InvalidRequestError as exc:
            status = "CONFIG_ERROR"
            error_message = str(exc)
        except Exception as exc:  # noqa: BLE001 - recorded on the attempt, retried below
            status = "ERROR"
            error_message = str(exc)

        elapsed = time.monotonic() - start
        attempt = {
            "platform": platform,
            "actor": actor_id,
            "attempt_number": attempt_number,
            "run_id": run_id,
            "dataset_id": dataset_id,
            "status": status,
            "started_at": started_at,
            "finished_at": now_iso(),
            "elapsed_seconds": round(elapsed, 2),
            "records_returned": len(items),
            "error": error_message,
        }
        attempts.append(attempt)

        if status == "CONFIG_ERROR":
            print_fn(f"Configuration error: {error_message}")
            print_fn("Not retrying: Apify rejected the run input.")
            return [], attempts, "CONFIG_ERROR", retry_wait_seconds

        if status == "SUCCEEDED" and items:
            print_fn(f"Received: {len(items)}")
            print_fn(f"Time: {elapsed:.1f} sec")
            return items, attempts, "PASS", retry_wait_seconds

        if status == "SUCCEEDED" and not items:
            print_fn("Received: 0")
            print_fn("Result considered unsuccessful")
        elif status != "TIMED_OUT":
            print_fn(f"Attempt {attempt_number} failed: {describe_attempt_failure(attempt)}")

        if attempt_number < MAX_ATTEMPTS:
            wait_s = RETRY_WAIT_SCHEDULE[attempt_number]
            retry_wait_seconds += wait_s
            print_fn(f"Waiting {wait_s} sec before retry...")
            time.sleep(wait_s)

    final_status = "NO_RESULTS" if attempts[-1]["status"] == "SUCCEEDED" else "FAILED"
    return [], attempts, final_status, retry_wait_seconds


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def normalize_tiktok_item(item, niche, fetched_at):
    author = item.get("authorMeta") or {}
    video_meta = item.get("videoMeta") or {}

    handle = author.get("name")
    creator = {
        "id": stringify(author.get("id")),
        "handle": handle,
        "profile_url": author.get("profileUrl") or (f"https://www.tiktok.com/@{handle}" if handle else None),
        "follower_count": author.get("fans"),
        "niche_or_category": niche,
    }
    video = {
        "id": stringify(item.get("id")),
        "url": item.get("webVideoUrl"),
        "view_count": item.get("playCount"),
        "like_count": item.get("diggCount"),
        "comment_count": item.get("commentCount"),
        "upload_timestamp": item.get("createTimeISO"),
        "duration_seconds": video_meta.get("duration"),
        "caption_text": item.get("text"),
        "thumbnail_url": video_meta.get("coverUrl"),
    }
    return {
        "platform": "tiktok",
        "niche": niche,
        "fetched_at": fetched_at,
        "creator": creator,
        "video": video,
    }


def normalize_youtube_item(item, niche, fetched_at):
    handle = item.get("channelUsername") or item.get("channelName")
    creator = {
        "id": stringify(item.get("channelId")),
        "handle": handle,
        "profile_url": item.get("channelUrl"),
        "follower_count": item.get("numberOfSubscribers"),
        "niche_or_category": niche,
    }
    video = {
        "id": stringify(item.get("id")),
        "url": item.get("url"),
        "view_count": item.get("viewCount"),
        "like_count": item.get("likes"),
        "comment_count": item.get("commentsCount"),
        "upload_timestamp": item.get("date"),
        "duration_seconds": parse_duration_to_seconds(item.get("duration")),
        "caption_text": item.get("title"),
        "thumbnail_url": item.get("thumbnailUrl"),
    }
    return {
        "platform": "youtube",
        "niche": niche,
        "fetched_at": fetched_at,
        "creator": creator,
        "video": video,
    }


def normalize_instagram_item(item, profile_map, niche, fetched_at):
    username = get_first(item, ["ownerUsername", "username", "ownerName"])
    profile = profile_map.get(username, {}) if username else {}

    profile_url = get_first(profile, ["url", "profileUrl"]) or (
        f"https://www.instagram.com/{username}/" if username else None
    )
    creator = {
        "id": stringify(get_first(profile, ["id", "userId"]) or get_first(item, ["ownerId"])),
        "handle": username,
        "profile_url": profile_url,
        "follower_count": get_first(profile, ["followersCount", "followerCount", "followers"]),
        "niche_or_category": niche,
    }

    short_code = item.get("shortCode")
    video_url = get_first(item, ["url"]) or (f"https://www.instagram.com/reel/{short_code}/" if short_code else None)
    video = {
        "id": stringify(get_first(item, ["id", "shortCode", "pk"])),
        "url": video_url,
        "view_count": get_first(item, ["videoPlayCount", "videoViewCount", "viewCount", "playsCount"]),
        "like_count": get_first(item, ["likesCount", "likeCount"]),
        "comment_count": get_first(item, ["commentsCount", "commentCount"]),
        "upload_timestamp": get_first(item, ["timestamp", "takenAt"]),
        "duration_seconds": parse_duration_to_seconds(get_first(item, ["videoDuration", "duration"])),
        "caption_text": get_first(item, ["caption"]),
        "thumbnail_url": get_first(item, ["displayUrl", "thumbnailUrl"]),
    }
    return {
        "platform": "instagram",
        "niche": niche,
        "fetched_at": fetched_at,
        "creator": creator,
        "video": video,
    }


# ---------------------------------------------------------------------------
# Platform runners
# ---------------------------------------------------------------------------

def status_line(name, status, attempts_used):
    if status == "PASS" and attempts_used == 1:
        return f"{name}: PASS"
    return f"{name}: {status} after {attempts_used} attempt{'s' if attempts_used != 1 else ''}"


def run_tiktok(client, niche, limit, raw_dir, normalized_dir, fetched_at):
    print()
    print("TikTok")
    print("-" * 60)

    run_input = {
        "searchQueries": [niche],
        "searchSection": "/video",
        "resultsPerPage": limit,
        "scrapeRelatedSearchWords": False,
    }
    items, attempts, status, retry_wait_seconds = run_actor_with_retries(
        client, "tiktok", TIKTOK_ACTOR, run_input, print
    )
    print()
    print(status_line("TikTok", status, len(attempts)))

    total_seconds = sum(a["elapsed_seconds"] for a in attempts) + retry_wait_seconds

    if status != "PASS":
        return {"status": status, "actor": TIKTOK_ACTOR, "attempts": attempts, "total_seconds": total_seconds}

    save_json(raw_dir / "tiktok.json", items)
    normalized = [normalize_tiktok_item(item, niche, fetched_at) for item in items]
    save_json(normalized_dir / "tiktok.json", normalized)
    creators = {r["creator"]["handle"] for r in normalized if r["creator"]["handle"]}

    return {
        "status": "PASS",
        "actor": TIKTOK_ACTOR,
        "records": normalized,
        "video_count": len(normalized),
        "creator_count": len(creators),
        "seconds": attempts[-1]["elapsed_seconds"],
        "dataset_id": attempts[-1]["dataset_id"],
        "attempts": attempts,
        "total_seconds": total_seconds,
    }


def run_youtube(client, niche, limit, raw_dir, normalized_dir, fetched_at):
    print()
    print("YouTube")
    print("-" * 60)

    run_input = {
        "searchQueries": [niche],
        "maxResults": 0,
        "maxResultsShorts": limit,
        "maxResultStreams": 0,
    }
    items, attempts, status, retry_wait_seconds = run_actor_with_retries(
        client, "youtube", YOUTUBE_ACTOR, run_input, print
    )
    print()
    print(status_line("YouTube", status, len(attempts)))

    total_seconds = sum(a["elapsed_seconds"] for a in attempts) + retry_wait_seconds

    if status != "PASS":
        return {"status": status, "actor": YOUTUBE_ACTOR, "attempts": attempts, "total_seconds": total_seconds}

    save_json(raw_dir / "youtube_shorts.json", items)
    normalized = [normalize_youtube_item(item, niche, fetched_at) for item in items]
    save_json(normalized_dir / "youtube.json", normalized)
    creators = {r["creator"]["handle"] for r in normalized if r["creator"]["handle"]}

    return {
        "status": "PASS",
        "actor": YOUTUBE_ACTOR,
        "records": normalized,
        "video_count": len(normalized),
        "creator_count": len(creators),
        "seconds": attempts[-1]["elapsed_seconds"],
        "dataset_id": attempts[-1]["dataset_id"],
        "attempts": attempts,
        "total_seconds": total_seconds,
    }


def run_instagram(client, niche, limit, raw_dir, normalized_dir, fetched_at):
    print()
    print("Instagram")
    print("-" * 60)

    search_input = {
        "search": niche,
        "searchType": "popular",
        "searchLimit": limit,
        "enhanceUserSearchWithFacebookPage": False,
    }
    reel_items, discovery_attempts, discovery_status, discovery_wait = run_actor_with_retries(
        client, "instagram", IG_SEARCH_ACTOR, search_input, print
    )
    print()
    print(status_line("Instagram", discovery_status, len(discovery_attempts)))

    discovery_seconds = sum(a["elapsed_seconds"] for a in discovery_attempts) + discovery_wait

    if discovery_status != "PASS":
        # Zero results (or a hard failure) after all retries — no usernames to
        # enrich, so profile enrichment never runs; Instagram is FAILED/NO_RESULTS.
        return {
            "status": discovery_status,
            "actor": IG_SEARCH_ACTOR,
            "discovery_attempts": discovery_attempts,
            "profile_attempts": [],
            "total_seconds": discovery_seconds,
        }

    save_json(raw_dir / "instagram_reels.json", reel_items)

    usernames = sorted(
        {
            get_first(item, ["ownerUsername", "username", "ownerName"])
            for item in reel_items
            if get_first(item, ["ownerUsername", "username", "ownerName"])
        }
    )

    profile_items = []
    profile_attempts = []
    profile_wait = 0.0
    if usernames:
        print()
        print("Instagram profile enrichment")
        print("-" * 60)
        profile_items, profile_attempts, profile_status, profile_wait = run_actor_with_retries(
            client, "instagram", IG_PROFILE_ACTOR, {"usernames": usernames}, print
        )
        print()
        print(status_line("Instagram profile enrichment", profile_status, len(profile_attempts)))

    save_json(raw_dir / "instagram_profiles.json", profile_items)

    profile_map = {}
    for profile in profile_items:
        uname = get_first(profile, ["username", "handle"])
        if uname:
            profile_map[uname] = profile

    normalized = [normalize_instagram_item(item, profile_map, niche, fetched_at) for item in reel_items]
    save_json(normalized_dir / "instagram.json", normalized)
    creators = {r["creator"]["handle"] for r in normalized if r["creator"]["handle"]}

    profile_seconds = sum(a["elapsed_seconds"] for a in profile_attempts) + profile_wait
    successful_profile_attempt = next((a for a in reversed(profile_attempts) if a["status"] == "SUCCEEDED"), None)

    return {
        "status": "PASS",
        "actor": f"{IG_SEARCH_ACTOR} + {IG_PROFILE_ACTOR}",
        "records": normalized,
        "video_count": len(normalized),
        "creator_count": len(creators),
        "search_seconds": discovery_attempts[-1]["elapsed_seconds"],
        "profile_seconds": successful_profile_attempt["elapsed_seconds"] if successful_profile_attempt else 0.0,
        "seconds": discovery_attempts[-1]["elapsed_seconds"]
        + (successful_profile_attempt["elapsed_seconds"] if successful_profile_attempt else 0.0),
        "search_dataset_id": discovery_attempts[-1]["dataset_id"],
        "profile_dataset_id": successful_profile_attempt["dataset_id"] if successful_profile_attempt else None,
        "profile_count": len(profile_items),
        "discovery_attempts": discovery_attempts,
        "profile_attempts": profile_attempts,
        "total_seconds": discovery_seconds + profile_seconds,
    }


# ---------------------------------------------------------------------------
# CLI / orchestration
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Apify short-video discovery probe (V0)")
    parser.add_argument("--niche", type=str, default=None)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--platform",
        type=str,
        choices=["tiktok", "instagram", "youtube", "all"],
        default="all",
    )
    return parser.parse_args()


def platforms_to_run(choice):
    if choice == "all":
        return ["tiktok", "instagram", "youtube"]
    return [choice]


def collect_error_entries(result):
    """Every attempt that didn't count as a pass, across every stage of a platform's result."""
    stages = []
    if "attempts" in result:
        stages.append(result["attempts"])
    if "discovery_attempts" in result:
        stages.append(result["discovery_attempts"])
    if "profile_attempts" in result:
        stages.append(result["profile_attempts"])

    entries = []
    for attempts in stages:
        for attempt in attempts:
            if attempt["status"] == "SUCCEEDED" and attempt["records_returned"] > 0:
                continue
            entries.append(
                {
                    "platform": attempt["platform"],
                    "actor": attempt["actor"],
                    "attempt": attempt["attempt_number"],
                    "run_id": attempt["run_id"],
                    "status": attempt["status"],
                    "elapsed_seconds": attempt["elapsed_seconds"],
                    "message": describe_attempt_failure(attempt),
                }
            )
    return entries


def main():
    args = parse_args()

    niche = args.niche
    if not niche:
        niche = input("Enter niche: ").strip()
    if not niche:
        print("Error: niche cannot be empty.")
        sys.exit(1)

    limit = args.limit
    if limit <= 0:
        print("Error: --limit must be a positive integer.")
        sys.exit(1)

    load_dotenv()
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        print("Error: APIFY_TOKEN not set. Copy .env.example to .env and add your token.")
        sys.exit(1)

    client = ApifyClient(token)

    requested_platforms = platforms_to_run(args.platform)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = DATA_DIR / f"{timestamp}_{slugify(niche)}"
    raw_dir = run_dir / "raw"
    normalized_dir = run_dir / "normalized"

    print("=" * 68)
    print("APIFY SOCIAL DISCOVERY PROBE")
    print("=" * 68)
    print()
    print(f"Niche: {niche}")
    print(f"Requested: {limit} videos/platform")
    print(f"Actor timeout: {ACTOR_TIMEOUT_SECONDS} sec | Max attempts: {MAX_ATTEMPTS}")

    program_start = time.monotonic()
    fetched_at = now_iso()

    results = {}
    errors = []
    runners = {
        "tiktok": ("TikTok", TIKTOK_ACTOR, run_tiktok),
        "instagram": ("Instagram", f"{IG_SEARCH_ACTOR} + {IG_PROFILE_ACTOR}", run_instagram),
        "youtube": ("YouTube", YOUTUBE_ACTOR, run_youtube),
    }

    for key in requested_platforms:
        _, _, runner = runners[key]
        result = runner(client, niche, limit, raw_dir, normalized_dir, fetched_at)
        results[key] = result
        errors.extend(collect_error_entries(result))

    save_json(run_dir / "errors.json", errors)

    all_records = []
    for key in requested_platforms:
        result = results[key]
        if result["status"] == "PASS":
            all_records.extend(result["records"])
    save_json(normalized_dir / "all_results.json", all_records)

    total_execution_seconds = time.monotonic() - program_start

    summary_platforms = {}
    passed_seconds = []
    total_videos = 0
    total_creators = 0

    for key in requested_platforms:
        result = results[key]
        total_seconds = round(result["total_seconds"], 2)

        if result["status"] != "PASS":
            if key == "instagram":
                summary_platforms[key] = {
                    "status": result["status"],
                    "discovery_attempts": len(result["discovery_attempts"]),
                    "profile_attempts": len(result["profile_attempts"]),
                    "total_platform_seconds": total_seconds,
                }
            else:
                summary_platforms[key] = {
                    "status": result["status"],
                    "attempts": len(result["attempts"]),
                    "total_platform_seconds": total_seconds,
                }
            continue

        total_videos += result["video_count"]
        total_creators += result["creator_count"]

        if key == "instagram":
            summary_platforms[key] = {
                "status": "PASS",
                "videos": result["video_count"],
                "creators": result["creator_count"],
                "discovery_attempts": len(result["discovery_attempts"]),
                "profile_attempts": len(result["profile_attempts"]),
                "search_seconds": round(result["search_seconds"], 2),
                "profile_seconds": round(result["profile_seconds"], 2),
                "total_platform_seconds": total_seconds,
            }
        else:
            summary_platforms[key] = {
                "status": "PASS",
                "videos": result["video_count"],
                "creators": result["creator_count"],
                "attempts": len(result["attempts"]),
                "successful_attempt_seconds": round(result["seconds"], 2),
                "total_platform_seconds": total_seconds,
            }
        passed_seconds.append(result["seconds"])

    average_platform_seconds = round(sum(passed_seconds) / len(passed_seconds), 2) if passed_seconds else None

    summary = {
        "niche": niche,
        "requested_per_platform": limit,
        "platforms": summary_platforms,
        "total_videos": total_videos,
        "total_creators": total_creators,
        "average_platform_seconds": average_platform_seconds,
        "total_execution_seconds": round(total_execution_seconds, 2),
    }
    save_json(run_dir / "summary.json", summary)

    print()
    print("=" * 68)
    print("SUMMARY")
    print("=" * 68)
    print()
    for key in requested_platforms:
        result = results[key]
        name = runners[key][0]
        if result["status"] != "PASS":
            print(f"{name:<12} {result['status']}")
            continue
        print(f"{name:<12} {result['video_count']:>2} videos     {result['creator_count']:>2} creators      {result['seconds']:.2f} sec")

    print()
    print(f"Total videos:             {total_videos}")
    print(f"Unique creators:          {total_creators}")
    if average_platform_seconds is not None:
        print(f"Average platform time:    {average_platform_seconds:.2f} sec")
    else:
        print("Average platform time:    n/a (no platform succeeded)")
    print(f"Total execution time:     {total_execution_seconds:.2f} sec")
    print()
    print("Combined JSON:")
    print(str(normalized_dir / "all_results.json"))
    print()
    print("Summary JSON:")
    print(str(run_dir / "summary.json"))

    if errors:
        print()
        print("Errors JSON:")
        print(str(run_dir / "errors.json"))


if __name__ == "__main__":
    main()
