# Steam second-success radar

A daily collector (no LLM) gathers Steam data for new and recently released games into SQLite. Later
chunks detect fast-growing small-team games and write a weekly digest. It runs on GitHub Actions; the
database lives as a gzipped asset of the rolling release `db-latest`.

## Data sources
- **Steam Web API** (terms: https://steamcommunity.com/dev/apiterms): app list, store metadata, tags,
  current players, review counts. Counts only; no reviewer data is stored.
- **Twitch Helix**: live viewers/channels only, deleted after 24 h because the developer agreement
  (https://legal.twitch.com/legal/developer-agreement) limits caching to 24 h.

Deliberately NOT used:
- SteamDB: terms forbid scraping; no public API.
- SteamCharts: scraping a third party site.
- SullyGnome / TwitchTracker: scraped Twitch history, which the 24 h clause rules out.
- SteamSpy / Gamalytic: estimated third-party numbers; we want first-party counts.

## Setup
1. Steam Web API key: https://steamcommunity.com/dev/apikey (the account must not be limited; any
   domain works, e.g. the repo URL).
2. Twitch app: https://dev.twitch.tv/console/apps (2FA required; category "Application Integration");
   note the client ID and secret.
3. Anthropic API key: https://platform.claude.com. Buy $20 prepaid credit, keep auto-reload OFF, and set a
   $20 monthly spend limit in the Console.
4. Two GitHub repos. This code runs from a **public** repo, because GitHub Actions minutes are free and
   unlimited there and a private repo's 2,000 a month do not cover a daily 50-minute collection. Your
   brief, digests and ledger live in a separate **private data repo** (`BRIEF.md`, `digests/`,
   `ledger.jsonl`, `logs/`); its name is `DATA_REPO` in `.github/workflows/weekly.yml`.
   In the public repo, Settings -> Secrets and variables -> Actions: `STEAM_API_KEY`, `ANTHROPIC_API_KEY`,
   `DATA_REPO_TOKEN` (a fine-grained token for the data repo only: Contents and Issues read/write), and
   optionally `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`. Secrets never reach a public repo's logs or forks;
   the workflows run only on a schedule or by hand, never on pull requests. Public logs carry counts and
   public Steam names only: the digest's own output goes to the data repo's `logs/`.
5. Run the workflow once by hand: Actions -> daily -> Run workflow.

## Workflows
- **daily** (05:17 UTC): collects Steam data (and a Twitch snapshot) into `radar.db`. No LLM.
- **backfill** (manual, once): builds review history for games released before the collector existed.
  Actions -> backfill -> Run workflow; `max_minutes` (default 300) stops it cleanly after that long. It is
  resumable: run it again and it carries on where it stopped. It ends by running detection.
- **weekly** (Sundays 06:47 UTC): runs detection, snapshots Twitch for the signal games, writes the digest,
  commits `digests/<date>.md`, `ledger.jsonl` and its log to the private data repo, and opens an issue
  "Radar digest <date>" there.
  Manual run: Actions -> weekly -> Run workflow. Tick `no_llm` to skip every Anthropic call: the digest then
  only lists the signals, unvetted, and spends nothing.

## Reading the digest and ledger
- `digests/<date>.md`: at most 3 concepts that survived the Skeptic, each with its inspirations table,
  numbers that each link to the store page they came from, the prototype plan and the scope estimate
  against the hours in `BRIEF.md`. Then "Killed this week" with the failing check, and "Notes" (data limits,
  clusters skipped, removed claims, errors). Zero concepts is a normal week; nothing is padded.
- A banner at the top means an LLM stage stopped (out of credit, spend cap, API error); the remaining
  signals are listed unvetted and the weekly job is marked failed.
- Every number in a concept is checked in code against the signal data and must carry its source link;
  a card with an unbacked number is dropped.
- `ledger.jsonl`: one line per concept judged (keep, kill, invalid, refused). A cluster already in the ledger,
  or whose candidate games were all judged in the last 26 weeks, is not judged again. Edit `BRIEF.md` any time:
  the Skeptic reads it every week.

## Cost guards
- Prepaid Anthropic credit with auto-reload off: a hard ceiling.
- Console monthly spend limit ($20).
- The code's own caps in `radar/config.py`: $16 per month (about EUR 15) and $4 per run. Before every call the
  worst case (input estimate plus the full output limit) is added to the `spend` table's total; if it would
  pass a cap the call is not made. After each call the real cost is written to `spend`.
- GitHub Actions spending limit of $0.

Expected total: EUR 3-7 per month.

## What the first digests cannot know yet
Player-count history (`ccu`) starts with the first daily run, so a trend needs 2-3 weeks of daily data; until
then the Skeptic judges mostly on reviews. Twitch data exists only for games snapshotted in the weekly run,
and only for 24 hours (the Twitch terms). The digest's Notes say how many days of history it had.

## Local run
```
$env:STEAM_API_KEY="..."; python -m radar.collect --db radar.db --limit 200
```
Without a key, `GetAppList` is skipped; seed appids by hand: `python -m radar.collect --db x.db --appids 730,3000000`.
Keyless live check: `python -m radar.smoke 730 413150`. Tests: `python -m pytest -q`.
