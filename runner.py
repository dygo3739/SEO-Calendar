"""
runner.py  —  Content Calendar Runner
Reads sites_config.json (exported from the HTML calendar app), runs each
site that is due today in sequence with a configurable gap between them to
stagger Anthropic API calls, then writes topic_idx and last_run back to the
config file so the calendar stays in sync.

Setup
─────
1. Export sites_config.json from the calendar app (Export button).
2. Place runner.py, engine.py, and sites_config.json in the same directory.
3. Set environment variables (once, in your shell profile or cron environment):

       export ANTHROPIC_API_KEY="sk-ant-..."
       export UNSPLASH_ACCESS_KEY="your-unsplash-key"

   WordPress credentials come from sites_config.json — no env var needed.

4. Install the only dependency:
       pip install requests   # or: pip3 install requests

5. Test without publishing:
       python runner.py --dry-run

6. Schedule with cron. Run it once a day, slightly before your earliest
   site's schedule_hour. E.g. if your first site runs at 7:00 UTC:

       55 6 * * * cd /path/to/project && python3 runner.py >> logs/runner.log 2>&1

   Or use GitHub Actions (see README) for fully serverless execution.

Usage
─────
    python runner.py                       # run all sites due today
    python runner.py --site "BizInZip"     # run one specific site
    python runner.py --all                 # force-run all active sites
    python runner.py --dry-run             # preview topics, no publishing
    python runner.py --gap 15              # override the inter-site gap (minutes)
"""

import json
import os
import sys
import time
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

import engine  # engine.py must be in the same directory

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("runner")

CONFIG_FILE      = Path(__file__).parent / "sites_config.json"
DEFAULT_GAP_SECS = 30 * 60   # 30 minutes between sites


# ── Config I/O ─────────────────────────────────────────────────────────────────
def load_config() -> list[dict]:
    if not CONFIG_FILE.exists():
        log.error(f"Config file not found: {CONFIG_FILE}")
        log.error("Export it from the Content Calendar app (Export button).")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        return json.load(f)


def save_config(sites: list[dict]):
    with open(CONFIG_FILE, "w") as f:
        json.dump(sites, f, indent=2, default=str)


def save_site(sites: list[dict], updated: dict):
    for i, s in enumerate(sites):
        if s["id"] == updated["id"]:
            sites[i] = updated
            break
    save_config(sites)


# ── Key normalisation ──────────────────────────────────────────────────────────
# The HTML app exports camelCase keys; engine.py uses snake_case internally.
# This function normalises a site dict so engine.py can use it directly.
def normalise(site: dict) -> dict:
    """Return a copy of site with both camelCase and snake_case keys present."""
    s = dict(site)
    mapping = {
        "wpUrl":          "wp_url",
        "wpUser":         "wp_username",
        "wpPass":         "wp_app_password",
        "schedHour":      "schedule_hour",
        "schedMin":       "schedule_min",
        "wordCount":      "word_count",
        "wpTagPool":      "wp_tag_pool",
        "allowedTagIds":  "allowed_tag_ids",
        "pillarPages":    "pillar_pages",
        "sitemapUrl":     "sitemap_url",
        "topic_idx":      "topic_idx",   # already snake_case from app
    }
    for camel, snake in mapping.items():
        if camel in s and snake not in s:
            s[snake] = s[camel]
        if snake in s and camel not in s:
            s[camel] = s[snake]
    # Ensure required WP fields have values
    for field in ("wp_url", "wp_username", "wp_app_password"):
        s[field] = (s.get(field) or "").strip()
    return s


# ── Topic helpers ──────────────────────────────────────────────────────────────
def pick_topic(site: dict) -> tuple[str, int]:
    """Return (raw_topic, next_idx). raw_topic may contain a | hint."""
    topics = site.get("topics", [])
    if not topics:
        raise ValueError(f"Site '{site['name']}' has no topics configured.")
    idx      = int(site.get("topic_idx", 0)) % len(topics)
    next_idx = (idx + 1) % len(topics)
    return topics[idx], next_idx


# ── Schedule logic ─────────────────────────────────────────────────────────────
def is_due_today(site: dict) -> bool:
    """
    True when the site should run today and hasn't already.
    Compares last_run date against today in UTC so timezone drift
    can't cause double-runs or missed runs.
    """
    now       = datetime.now(timezone.utc)
    today_str = now.date().isoformat()   # "2025-07-15"

    # Skip if already ran today
    last_run = site.get("last_run")
    if last_run:
        try:
            last_date = datetime.fromisoformat(
                last_run.replace("Z", "+00:00")
            ).date().isoformat()
            if last_date == today_str:
                return False
        except ValueError:
            pass  # malformed date — treat as not run

    freq = site.get("freq", "daily")

    if freq == "weekly":
        # schedule_weekday: 0=Sun … 6=Sat  (matches JS Date.getUTCDay())
        sched_day = int(site.get("schedule_weekday", site.get("schedWeekday", 1)))
        if now.weekday() != (sched_day - 1) % 7:
            return False

    if freq == "biweekly":
        start_str = site.get("start_date", today_str)
        try:
            start = datetime.fromisoformat(start_str).date()
        except ValueError:
            start = now.date()
        days_since = (now.date() - start).days
        if days_since % 14 != 0:
            return False

    return True


def sites_due(sites: list[dict]) -> list[dict]:
    return [s for s in sites if s.get("active", True) and is_due_today(s)]


# ── Per-site pipeline ──────────────────────────────────────────────────────────
def run_site(site: dict, dry_run: bool = False) -> bool:
    name = site["name"]
    log.info(f"━━━  {name}  {'━' * max(0, 50 - len(name))}")

    site = normalise(site)

    # Validate WP credentials exist
    if not site["wp_url"] or not site["wp_app_password"]:
        log.error(f"{name}: WordPress credentials missing — skipping")
        return False

    # Pick topic
    try:
        topic_raw, next_idx = pick_topic(site)
    except ValueError as e:
        log.error(str(e))
        return False

    label, hint = engine.parse_topic(topic_raw)
    log.info(f"Topic [{site.get('topic_idx', 0) + 1}/{len(site['topics'])}]: {label}")
    if hint:
        log.info(f"Focus: {hint}")

    if dry_run:
        log.info("Dry run — skipping publish")
        return True

    try:
        # 1. Fetch sitemap URLs (non-fatal if it fails)
        sitemap_url = site.get("sitemap_url") or site.get("sitemapUrl", "")
        sitemap_urls = []
        if sitemap_url:
            log.info("Fetching sitemap…")
            sitemap_urls = engine.fetch_sitemap_urls(sitemap_url)

        # 2. Write article
        log.info("Writing article via Claude…")
        article = engine.write_article(topic_raw, site, sitemap_urls)
        log.info(f"Title:    {article['title']}")
        log.info(f"Keyphrase: {article.get('yoast_keyphrase', '—')}")
        log.info(f"Meta ({len(article.get('yoast_meta',''))} chars): {article.get('yoast_meta','—')[:80]}")

        # 3. Fetch photo
        search_term = article.get("search_term", label[:40])
        log.info(f"Fetching photo: {search_term}")
        photo = engine.fetch_unsplash_photo(search_term)
        if photo:
            log.info(f"Photo: {photo['photographer']} / Unsplash")
            engine.trigger_unsplash_download(photo["download_url"])
        else:
            log.warning("No photo found — continuing without featured image")

        # 4. Upload photo
        media_id = None
        if photo:
            log.info("Uploading photo to WordPress…")
            media_id = engine.upload_photo(site, photo)
            if media_id:
                log.info(f"Media ID: {media_id}")

        # 5. Publish
        log.info("Publishing to WordPress…")
        post_url = engine.publish_post(site, article, media_id, photo)
        log.info(f"Published: {post_url}")

        return True

    except Exception as e:
        log.error(f"Pipeline failed for {name}: {e}", exc_info=True)
        return False


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Content Calendar Runner")
    parser.add_argument("--site",    help="Run a single site by name")
    parser.add_argument("--all",     action="store_true", help="Force-run all active sites")
    parser.add_argument("--dry-run", action="store_true", help="Preview without publishing")
    parser.add_argument("--gap",     type=int, default=DEFAULT_GAP_SECS // 60,
                        help="Minutes to wait between sites (default: 30)")
    args = parser.parse_args()

    gap_secs = args.gap * 60

    log.info(f"Content Calendar Runner — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    sites = load_config()

    # Determine targets
    if args.site:
        targets = [s for s in sites if s["name"].lower() == args.site.lower()]
        if not targets:
            log.error(f"Site '{args.site}' not found in config.")
            log.info(f"Available: {[s['name'] for s in sites]}")
            sys.exit(1)
    elif args.all:
        targets = [s for s in sites if s.get("active", True)]
    else:
        targets = sites_due(sites)

    if not targets:
        log.info("No sites due to run today. Exiting.")
        log.info("Use --all to force-run all sites, or --site <name> for one site.")
        return

    log.info(f"Sites to run ({len(targets)}): {[s['name'] for s in targets]}")

    results: dict[str, bool] = {}

    for i, site in enumerate(targets):
        if i > 0:
            log.info(f"Waiting {args.gap} min before next site…")
            if not args.dry_run:
                time.sleep(gap_secs)

        success = run_site(site, dry_run=args.dry_run)
        results[site["name"]] = success

        if success and not args.dry_run:
            _, next_idx = pick_topic(site)
            site["topic_idx"] = next_idx
            site["last_run"]  = datetime.now(timezone.utc).isoformat()
            save_site(sites, site)
            log.info(f"Config saved — next topic: #{next_idx + 1}")

    # Summary
    log.info("━━━  Summary  " + "━" * 40)
    for name, ok in results.items():
        log.info(f"  {'✓' if ok else '✗'}  {name}")

    failed = [n for n, ok in results.items() if not ok]
    if failed:
        log.error(f"{len(failed)} site(s) failed.")
        sys.exit(1)
    else:
        log.info("All done.")


if __name__ == "__main__":
    main()
