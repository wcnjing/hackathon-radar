import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from hackathon_radar.config import db_path, load_config
from hackathon_radar.enrich import enrich_events
from hackathon_radar.filtering import in_scope, normalize_title
from hackathon_radar.notify import Telegram, TelegramError, format_message, is_quiet_hour
from hackathon_radar.scoring import make_client, score_events
from hackathon_radar.sources import fetch_all
from hackathon_radar.store import Store

log = logging.getLogger("radar")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _collect(config: dict, store: Store):
    """Fetch all sources and score whatever is new. No writes."""
    scope_cfg = config.get("scope", {})
    include_full = scope_cfg.get("include_full", False)
    events = fetch_all(config)
    scoped = [e for e in events if in_scope(e, scope_cfg)]
    joinable = [e for e in scoped if include_full or not e.full]
    new = [e for e in joinable if not store.is_seen(e)]
    log.info(
        "fetched %d, in scope %d, joinable %d, new %d",
        len(events), len(scoped), len(joinable), len(new),
    )

    try:
        client = make_client()
    except Exception as exc:
        log.info("Anthropic credentials unavailable (%s); keyword scoring only", exc)
        client = None
    scores = score_events(new, config, client)
    return new, scores, client


def _select(new, scores, store: Store, config: dict, cap: int, dry_run: bool):
    """Rank new events and pick the ones worth posting; records the rest."""
    interests = config.get("interests", {})
    min_score = interests.get("min_score", 6)
    # e.g. { networking = 8 }: talks/mixers must be exceptional to earn a post.
    min_by_kind = interests.get("min_score_by_kind", {})
    notify_cfg = config.get("notify", {})
    dupe_days = notify_cfg.get("duplicate_title_days", 14)
    dupe_cutoff = _iso(_now() - timedelta(days=dupe_days))
    recent_titles = {
        normalize_title(t)
        for t in [*store.notified_titles_since(dupe_cutoff), *store.queued_titles()]
    }

    selected = []
    for event in sorted(new, key=lambda e: scores[e.key][0], reverse=True):
        score, reason = scores[event.key]
        norm_title = normalize_title(event.title)
        if norm_title and norm_title in recent_titles:
            log.info("skip (duplicate title): %s", event.title)
            if not dry_run:
                store.record(event, score, "skipped: same title as a recent notification")
            continue
        threshold = min_by_kind.get(event.kind, min_score)
        if score < threshold:
            log.info("skip (score %.0f < %d for %s): %s", score, threshold, event.kind, event.title)
            if not dry_run:
                store.record(event, score, reason)
            continue
        if len(selected) >= cap:
            log.info("skip (over cap): %s", event.title)
            if not dry_run:
                store.record(event, score, reason)
            continue
        recent_titles.add(norm_title)
        selected.append(event)
    return selected


def _ingest(config: dict, store: Store) -> None:
    """Fetch, score, enrich, and queue notification-worthy events."""
    new, scores, client = _collect(config, store)
    # The daily cap bounds how much one ingest may queue; anything beyond it
    # is recorded as seen (same policy as the old per-run cap).
    cap = config.get("notify", {}).get("max_per_day", 15)
    selected = _select(new, scores, store, config, cap, dry_run=False)

    if config.get("enrich", {}).get("enabled", True):
        enrich_events(selected, config, client)
    for event in selected:
        score, reason = scores[event.key]
        store.queue_event(event, score, reason)
    store.set_meta("last_fetch_at", _iso(_now()))
    log.info("queued %d event(s); queue depth now %d", len(selected), store.queue_depth())


def _drain(config: dict, store: Store, telegram: Telegram) -> int:
    """Post at most one queued event, respecting pacing and the daily cap.
    Returns -1 on send failure, else the number of messages sent."""
    notify_cfg = config.get("notify", {})

    interval = notify_cfg.get("send_interval_seconds", 1800)
    last_send = store.get_meta("last_send_at")
    if last_send:
        elapsed = (_now() - datetime.fromisoformat(last_send)).total_seconds()
        if elapsed < interval:
            log.info("drip gap not elapsed (%.0fs of %ds); queue depth %d",
                     elapsed, interval, store.queue_depth())
            return 0

    day_ago = _iso(_now() - timedelta(days=1))
    if store.notified_count_since(day_ago) >= notify_cfg.get("max_per_day", 15):
        log.info("daily cap reached; queue depth %d", store.queue_depth())
        return 0

    dupe_days = notify_cfg.get("duplicate_title_days", 14)
    dupe_cutoff = _iso(_now() - timedelta(days=dupe_days))
    recent_titles = {normalize_title(t) for t in store.notified_titles_since(dupe_cutoff)}
    while True:
        item = store.pop_queued()
        if item is None:
            log.info("queue empty; nothing to send")
            return 0
        event, score, reason = item
        norm_title = normalize_title(event.title)
        if norm_title and norm_title in recent_titles:
            store.drop_queued(event, "skipped at send time: same title as a recent notification")
            log.info("drop queued duplicate title before send: %s", event.title)
            continue
        break

    local_hour = datetime.now(ZoneInfo(notify_cfg.get("timezone", "Asia/Singapore"))).hour
    silent = is_quiet_hour(
        local_hour, notify_cfg.get("quiet_start", 23), notify_cfg.get("quiet_end", 8)
    )
    try:
        telegram.send(
            format_message(event, team_prompt=notify_cfg.get("team_prompt", False)),
            silent=silent,
        )
    except TelegramError as exc:
        # Still queued (not marked notified) — retried next run.
        log.error("send failed, will retry next run: %s", exc)
        return -1
    store.mark_notified(event)
    store.set_meta("last_send_at", _iso(_now()))
    log.info("sent (%.0f/10): %s — queue depth now %d", score, event.title, store.queue_depth())
    return 1


def _preview(args: argparse.Namespace, config: dict, store: Store) -> int:
    """Dry run: fetch + score + show what would be queued. Writes nothing."""
    new, scores, client = _collect(config, store)
    cap = args.max_notify or config.get("notify", {}).get("max_per_run", 10)
    selected = _select(new, scores, store, config, cap, dry_run=True)
    if config.get("enrich", {}).get("enabled", True):
        enrich_events(selected, config, client)
    for event in selected:
        score, _ = scores[event.key]
        print(f"\n--- would queue ({score:.0f}/10) ---\n{format_message(event)}")
    log.info("%d event(s) would be queued (dry run)", len(selected))
    return 0


def run(args: argparse.Namespace) -> int:
    config = load_config()
    store = Store(db_path())

    if args.dry_run:
        result = _preview(args, config, store)
        store.close()
        return result

    telegram = Telegram()
    if not telegram.configured:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — use --dry-run or fill in .env")
        return 1

    # Sources are fetched every fetch_every_hours; in-between runs only drip
    # the queue, keeping each run short and the channel calm.
    fetch_every = config.get("notify", {}).get("fetch_every_hours", 6)
    last_fetch = store.get_meta("last_fetch_at")
    fetch_due = True
    if last_fetch:
        elapsed = (_now() - datetime.fromisoformat(last_fetch)).total_seconds()
        fetch_due = elapsed >= fetch_every * 3600 - 600  # 10 min scheduling tolerance
        if os.environ.get("RADAR_FORCE_FETCH", "").lower() in ("1", "true"):
            fetch_due = True
        if not fetch_due:
            log.info("fetch not due (%.0f min since last); drain-only run", elapsed / 60)
    if fetch_due:
        _ingest(config, store)

    sent = _drain(config, store, telegram)
    store.close()
    return 1 if sent < 0 else 0


def get_chat_id(args: argparse.Namespace) -> int:
    load_config()
    telegram = Telegram()
    if not telegram.token:
        log.error("TELEGRAM_BOT_TOKEN not set in .env")
        return 1
    updates = telegram.get_updates()
    chats = {}
    for update in updates:
        message = update.get("channel_post") or update.get("message") or {}
        chat = message.get("chat", {})
        if chat.get("id"):
            chats[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("type")
    if not chats:
        print(
            "No chats found. Post any message in your channel (with the bot added as admin),\n"
            "then run this again."
        )
        return 1
    for chat_id, name in chats.items():
        print(f"{chat_id}\t{name}")
    print("\nPut the channel's id in .env as TELEGRAM_CHAT_ID.")
    return 0


def test_telegram(args: argparse.Namespace) -> int:
    load_config()
    telegram = Telegram()
    if not telegram.configured:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in .env")
        return 1
    try:
        telegram.send("✅ hackathon-radar is connected. You'll get event alerts here.")
    except TelegramError as exc:
        log.error("%s", exc)
        return 1
    print("Sent — check your channel.")
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs at INFO; Telegram URLs embed the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="radar", description="Hackathon & event notifier")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="fetch/queue when due, then drip one queued event")
    p_run.add_argument("--dry-run", action="store_true", help="print instead of queueing/sending")
    p_run.add_argument("--max-notify", type=int, default=None, help="preview cap for --dry-run")
    p_run.set_defaults(func=run)

    p_chat = sub.add_parser("get-chat-id", help="discover your channel's chat id")
    p_chat.set_defaults(func=get_chat_id)

    p_test = sub.add_parser("test-telegram", help="send a test message to the channel")
    p_test.set_defaults(func=test_telegram)

    args = parser.parse_args()
    sys.exit(args.func(args))
