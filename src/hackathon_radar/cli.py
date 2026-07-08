import argparse
import logging
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


def run(args: argparse.Namespace) -> int:
    config = load_config()
    store = Store(db_path())
    telegram = Telegram()

    if not args.dry_run and not telegram.configured:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — use --dry-run or fill in .env")
        return 1

    events = fetch_all(config)
    scoped = [e for e in events if in_scope(e, config.get("scope", {}))]
    new = [e for e in scoped if not store.is_seen(e)]
    log.info("fetched %d, in scope %d, new %d", len(events), len(scoped), len(new))

    try:
        client = make_client()
    except Exception as exc:
        log.info("Anthropic credentials unavailable (%s); keyword scoring only", exc)
        client = None

    # Score first (cheap, batched); enrichment — one page-fetching Claude call per
    # event — is deferred until we know which events actually get posted.
    scores = score_events(new, config, client)
    min_score = config.get("interests", {}).get("min_score", 6)
    notify_cfg = config.get("notify", {})
    max_notify = args.max_notify or notify_cfg.get("max_per_run", 10)

    # Guardrail: rolling 24h cap across runs, so even a lost dedupe DB can't flood.
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    sent_today = store.notified_count_since(day_ago)
    budget = max(0, notify_cfg.get("max_per_day", 15) - sent_today)
    if budget < max_notify:
        log.info("daily cap: %d sent in last 24h, budget now %d", sent_today, budget)
        max_notify = budget

    # Guardrail: skip anything whose title matches a recent notification —
    # catches cross-source duplicates and recurring events with fresh ids.
    dupe_days = notify_cfg.get("duplicate_title_days", 14)
    dupe_cutoff = (
        datetime.now(timezone.utc) - timedelta(days=dupe_days)
    ).isoformat(timespec="seconds")
    recent_titles = {normalize_title(t) for t in store.notified_titles_since(dupe_cutoff)}

    # Guardrail: deliver silently during quiet hours (no phone ping overnight).
    local_hour = datetime.now(
        ZoneInfo(notify_cfg.get("timezone", "Asia/Singapore"))
    ).hour
    silent = is_quiet_hour(
        local_hour, notify_cfg.get("quiet_start", 23), notify_cfg.get("quiet_end", 8)
    )

    # First pass: pick the events that clear every gate; record the rest now.
    # Dry runs must not mark events as seen, or the first real run would
    # silently skip everything already previewed.
    ranked = sorted(new, key=lambda e: scores[e.key][0], reverse=True)
    to_notify = []
    for event in ranked:
        score, reason = scores[event.key]
        norm_title = normalize_title(event.title)
        if norm_title and norm_title in recent_titles:
            if not args.dry_run:
                store.record(event, score, "skipped: same title as a recent notification")
            continue
        if score < min_score or len(to_notify) >= max_notify:
            if not args.dry_run:
                store.record(event, score, reason)
            continue
        recent_titles.add(norm_title)
        to_notify.append(event)

    # Only now spend on enrichment — one page-fetching Claude call per event —
    # and only for the handful actually being posted.
    if config.get("enrich", {}).get("enabled", True):
        enrich_events(to_notify, config, client)

    # Second pass: send.
    for event in to_notify:
        score, reason = scores[event.key]
        message = format_message(event)
        if args.dry_run:
            print(f"\n--- would notify ({score:.0f}/10) ---\n{message}")
        else:
            try:
                telegram.send(message, silent=silent)
            except TelegramError as exc:
                # Not recorded as seen — it will be retried next run.
                log.error("send failed, will retry next run: %s", exc)
                store.close()
                return 1
            store.record(event, score, reason)
            store.mark_notified(event)

    log.info("notified %d event(s)%s", len(to_notify), " (dry run)" if args.dry_run else "")
    store.close()
    return 0


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

    p_run = sub.add_parser("run", help="fetch, score, and notify new events")
    p_run.add_argument("--dry-run", action="store_true", help="print instead of sending to Telegram")
    p_run.add_argument("--max-notify", type=int, default=None, help="cap notifications this run")
    p_run.set_defaults(func=run)

    p_chat = sub.add_parser("get-chat-id", help="discover your channel's chat id")
    p_chat.set_defaults(func=get_chat_id)

    p_test = sub.add_parser("test-telegram", help="send a test message to the channel")
    p_test.set_defaults(func=test_telegram)

    args = parser.parse_args()
    sys.exit(args.func(args))
