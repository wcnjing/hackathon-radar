# hackathon-radar — Project Doc

**Status: live.** Auto-curates hackathons, competitions, and tech events for
Singapore tech undergrads and drip-posts them to the Telegram channel
**@EventSonar**. Runs unattended on GitHub Actions; a Mac is never required.

*(Operational how-to lives in [README.md](README.md); this doc is the why,
the roadmap, and the engineering record.)*

## Vision

Every actionable opportunity for an SG tech student — hackathons, buildathons,
competitions, fellowships — in one calm, trustworthy channel, found
automatically, so nobody hears about a hackathon three days after registration
closed.

The differentiators over manually-curated lists:

1. **Automated**, so it never goes stale or depends on a committee member's free time
2. **Enriched** — each card carries what students actually decide on: deadline,
   team size, experience level, and the problem statement behind one tap
3. **Curated for one audience** — an AI scorer tuned to "techie undergrad who
   wants to level up", with actionable events (build/compete/apply) ranked
   above attend-and-mingle ones
4. **Calm by construction** — posts are ≥30 min apart, capped daily, silent at
   night; the channel respects attention, which is why people stay subscribed

## Strategy / roadmap

- **Phase 0 — the radar (done, hardening continues):** everything below in
  "What's implemented". Remaining hardening lives in "Known gaps".
- **Phase 1 — channel launch (current):** channel identity (name, photo,
  pinned intro), 2+ weeks of scrollback, then soft-launch to warm contacts and
  club committee members (NTUOSS, NUS Hackers, GDSC chapters). Growth engine is
  card *forwards* into group chats, not ads.
- **Phase 2 — community:** linked Telegram discussion group (one toggle) so
  each card gets a comment thread; members become a source (forwarding company
  emails to the feed inbox, suggesting watchlist URLs); polls to tune curation.
- **Phase 3 — teammate matchmaking (the possible destination):** the hardest
  part of joining a hackathon is finding a team. Natural evolution: per-event
  "looking for team" threads in the discussion group, then a bot flow — members
  DM the bot a short profile (skills, year, interests), tap "find teammates"
  on an event card, and get matched with others who tapped the same event.
  The existing machinery transfers: the scoring persona becomes a matching
  prompt, the queue/store pattern holds profiles and match state, and the bot
  identity (@EventSonarBot) already exists. This is also the moat — event
  listings can be copied; a network of matched teammates can't.
- **Phase 4 — optional expansions:** sibling internships channel (same radar,
  second chat id — kept out of the main channel by deliberate policy), other
  cities via the same source architecture (Luma city feeds are one config line).

## Expected outcomes

**For subscribers:** never miss a joinable, relevant event; each card decidable
in ~10 seconds; ≤15 posts/day, usually far fewer; zero spam or duplicates.

**For the owner:** zero-touch operation (no machine, no cron babysitting);
API + infra cost of roughly cents/month steady-state (see README "Cost");
a growing community asset and a strong portfolio story.

**System targets:** consecutive posts ≥30 min apart, always; only genuinely
new events; nothing full/closed/ended; networking posts only when exceptional
(score ≥8 vs ≥6 for hackathons); overnight posts silent; a lost cache bounded
to one re-notified day, never a flood.

## What's implemented

**Sources (5 pipelines):**
| Source | Mechanism |
|---|---|
| Devpost | Unofficial JSON API, open/upcoming only, `/register` links, invite-only flagged |
| MLH | Season pages (schema.org microdata), current + next season |
| Luma | SG discover feed via embedded `__NEXT_DATA__` + discover API; full/waitlist events dropped |
| Watchlist | Arbitrary organizer URLs (incl. `t.me/s/<channel>` for public Telegram channels); Claude extracts events only when a page's content-hash changes |
| Email | Dedicated Gmail inbox over read-only IMAP with UID tracking; Claude extracts events from newsletter bodies **and attachments** (.ics parsed, PDFs via pypdf, poster images via Claude vision); hyperlinks preserved as `text (url)` |

**Pipeline:** fetch → scope filter (SG + online; per-event country inference
for global pages) → joinable filter → seen-DB dedupe → Claude scoring
(claude-haiku-4-5, batched, structured outputs; classifies kind 🛠/🤝/🚀 and
experience level 🌱/⚡/🔥) → selection (kind-aware thresholds, repeat-title
guard, cap) → enrichment (Devpost pages: team size, deadline, expandable brief
— **only for events actually being posted**) → queue → **drip** (each run posts
≤1; cron fires every 30 min; sources re-fetched every 6h).

**Guardrails:** seen-DB; score thresholds (6, networking 8); 30-min drip;
15/day rolling cap; 14-day repeat-title guard; full-event filter; quiet hours
(23:00–08:00 SGT, silent delivery); failed sends stay queued and retry;
keyword-scorer fallback when the API is unavailable.

**Infra:** GitHub Actions (private repo), `concurrency` group serializes runs,
SQLite + JSON state persisted via actions/cache, secrets for all credentials,
keepalive step defeats the 60-day schedule auto-disable, `force_fetch`
dispatch input for on-demand sweeps. Cards are public-friendly: scores and
relevance reasons stay in the DB.

## Build / test / operate

```sh
uv sync                          # deps (Python 3.14, managed by uv)
uv run pytest                    # 66 tests, all offline (fixtures/mocks)
uv run radar run --dry-run       # preview: fetch + score + print, no posting
uv run radar test-telegram       # verify bot + channel wiring
uv run radar get-chat-id         # discover the channel id
```

Deploy = `git push` (the workflow picks up code + config on its next run).
Manual cloud run: `gh workflow run radar.yml` (add `-f force_fetch=true` to
sweep sources immediately). Logs: `gh run list` / `gh run view <id> --log` —
skips are logged with reasons (`skip (score 4 < 6)...`).

Secrets (GitHub + local `.env`): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
`ANTHROPIC_API_KEY`, `EMAIL_ADDRESS`, `EMAIL_APP_PASSWORD`. Never in code,
never in chat/screenshots; error paths and httpx logs are sanitized because a
bot token leaked via a traceback once (regenerate via @BotFather `/revoke`).

⚠️ **Dry-run caveat:** `--dry-run` writes nothing to the events DB, but the
watchlist/email fetchers still advance their own state (page hashes, IMAP
UID) — a local dry-run can consume events the cloud never sees. Known gap;
avoid casual local dry-runs while the cloud pipeline is live.

Cache reset (`gh cache delete`) = every current event looks new again → up to
one day's cap re-posts, and one full re-scoring/enrichment spend. Deliberate
action, not routine.

## Edge cases handled

- **Dedupe:** per-source ids; cross-source and recurring events (fresh Luma ids
  weekly) via normalized-title guard; unicode titles normalize to empty → guard
  skipped rather than everything colliding
- **Sources:** Devpost prize HTML stripped; invite-only kept but flagged;
  submission window labeled so a past start date doesn't read as "over"; MLH
  season rollover (fetch both years); Luma full/waitlist/sold-out dropped
  *before* dedupe so a freed-up event can still post later; stale organizer
  pages (year-less past dates skipped); global pages (Jane Street) get
  per-event country inference so NY events don't pass the SG filter; JS-only
  pages detected and rejected as watchlist candidates
- **Email:** app-password spaces stripped; plain-vs-HTML part chosen by link
  presence; linkless events skipped (non-actionable) — now rare since links
  survive extraction; per-message failure doesn't kill the batch; IMAP `UID n:*`
  returns-last-message quirk filtered
- **Delivery:** Telegram HTML escaping (incl. inside expandable quotes); errors
  never contain the token (it's embedded in API URLs); send-then-record
  ordering so failures retry rather than vanish; drip gap enforced across runs
  via DB timestamp, immune to run timing drift
- **Ops:** DB schema migrates in place (pre-queue DBs gain the payload column);
  concurrent runs impossible (concurrency group); missing credentials degrade
  each feature independently (keyword scoring / skip enrichment / skip source)
  rather than crashing

## Known gaps & open risks (from the 2026-07-10 code review; honest list)

- **Dry-run mutates source state** (watchlist hashes, email UID) — see caveat above
- **actions/cache saves only on job success** — a failed run rolls back the
  DB: re-spend on the next run, and a sent-but-rolled-back event would re-post
- **Repeat-title guard doesn't see still-queued titles** — same event from two
  sources within one drip backlog can post twice
- **Queued payloads embed the Event schema** — renaming/removing an Event field
  while events sit queued would crash drain until the cache is cleared
- **No queue staleness check** — under sustained cap pressure an event could
  post after its date has passed
- **Telegram transport errors** (timeouts, DNS) aren't caught like API errors —
  a network blip crashes the run instead of the graceful retry path
- **Anthropic outage during ingest** → keyword fallback scores are recorded
  permanently; events Claude would have loved are never re-scored
- **IMAP UIDVALIDITY reset unhandled** — a Gmail folder reset would silently
  mute the email source
- Housekeeping: bot token regeneration still pending; parts of README ops text
  predate the drip rework

## Decision log

- **GitHub Actions over launchd** — machine independence beat local simplicity
- **Queue + 30-min cron over in-run sleeps** — sleeping billed runner minutes
- **Haiku for scoring/extraction** — cost floor; quality sufficient for triage
- **Scores/reasons backend-only** — public channel gets factual cards
- **Networking threshold 8 vs 6** — actionable > attendable, enforced in code
- **Internships excluded** — dated-event-only policy; sibling channel if ever
- **Linkless events skipped** — a card pointing nowhere burns trust
