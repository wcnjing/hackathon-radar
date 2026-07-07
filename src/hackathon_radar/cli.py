import argparse
import logging
import sys

from hackathon_radar.config import db_path, load_config
from hackathon_radar.enrich import enrich_events
from hackathon_radar.filtering import KEYWORD_REASON_PREFIX, in_scope
from hackathon_radar.notify import Telegram, format_message
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
    enrich_events(new, config, client)
    scores = score_events(new, config, client)
    min_score = config.get("interests", {}).get("min_score", 6)
    max_notify = args.max_notify or config.get("notify", {}).get("max_per_run", 10)

    ranked = sorted(new, key=lambda e: scores[e.key][0], reverse=True)
    notified = 0
    for event in ranked:
        score, reason = scores[event.key]
        if not args.dry_run:
            # Dry runs must not mark events as seen, or the first real run
            # would silently skip everything already previewed.
            store.record(event, score, reason)
        if score < min_score or notified >= max_notify:
            continue
        # Keyword-scorer reasons are DB debug detail, not worth a line on the card.
        display_reason = "" if reason.startswith(KEYWORD_REASON_PREFIX) else reason
        message = format_message(event, score, display_reason)
        if args.dry_run:
            print(f"\n--- would notify ({score:.0f}/10) ---\n{message}")
        else:
            telegram.send(message)
            store.mark_notified(event)
        notified += 1

    log.info("notified %d event(s)%s", notified, " (dry run)" if args.dry_run else "")
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
    telegram.send("✅ hackathon-radar is connected. You'll get event alerts here.")
    print("Sent — check your channel.")
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
