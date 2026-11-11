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
3. Anthropic API key: https://platform.claude.com. Use prepaid credit with auto-reload OFF and set the Console
   monthly spend limit to at least $60 (the code's own cap is $50; the account's credits are $200 a month).
4. Two GitHub repos. This code runs from a **public** repo, because GitHub Actions minutes are free and
   unlimited there and a private repo's 2,000 a month do not cover a daily 50-minute collection. Your
   brief, digests and ledger live in a separate **private data repo** (`BRIEF.md`, `digests/`,
   `ledger.jsonl`, `logs/`); its name is `DATA_REPO` in `.github/workflows/weekly.yml`.
   In the public repo, Settings -> Secrets and variables -> Actions: `STEAM_API_KEY`, `ANTHROPIC_API_KEY`,
   `DATA_REPO_TOKEN` (a fine-grained token for the data repo only: Contents and Issues read/write), and
   optionally `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`. Secrets never reach a public repo's logs or forks;
   the workflows run only on a schedule or by hand, never on pull requests. Public logs carry counts and
   public Steam names only: the digest's own output goes to the data repo's `logs/`.
5. Phone pushes (optional, free): install the ntfy app (https://ntfy.sh), subscribe to a long random topic
   name, and save the same name as the `NTFY_TOPIC` secret. Actions -> notify-test sends a test push. Then
   the daily run pushes one line each morning (what was collected, how many young games are growing fast,
   possible signals) and the weekly run one on Sunday (how many concepts survived, the API spend), with a
   high-priority push when a run fails. Messages hold counts and public game names, never a concept.
6. Run the workflow once by hand: Actions -> daily -> Run workflow.

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

## The weekly funnel
For each cluster (up to 20, best score first): **player voice** (Sonnet summarises the newest English reviews of its
two strongest candidates: loves, complaints, wishes; quotes are checked verbatim in code) -> **Scout** (Sonnet, one
concept card; every number checked against the signal data) -> **Skeptic A** (Opus) on up to 15 cards; its kill is
final. For A's survivors only: **web research** (Sonnet with web search and fetch: why the games grow, whether a
publisher is behind them, whether the twist already exists; a finding counts only with a page that was really
fetched) -> **Skeptic B** (Opus, independent: it never sees A's verdict, it does see the research; its claims may
cite a fetched page if the number is on it). A concept is kept only if B keeps it.

## Reading the digest and ledger
- `digests/<date>.md`: at most 3 concepts that survived both Skeptics, each with its inspirations table,
  a Research list of findings with links, numbers that each link to the page they came from, the prototype plan and the scope estimate
  against the hours in `BRIEF.md`. Then "Killed this week" with the failing check and which Skeptic killed it, and "Notes" (data limits,
  clusters skipped, removed claims, errors). Zero concepts is a normal week; nothing is padded.
- A banner at the top means an LLM stage stopped (out of credit, spend cap, API error); the remaining
  signals are listed unvetted and the weekly job is marked failed.
- Every number in a concept is checked in code against the signal data and must carry its source link;
  a card with an unbacked number is dropped.
- `ledger.jsonl`: one line per concept judged (keep, kill, invalid, refused). A cluster already in the ledger,
  or whose candidate games were all judged in the last 26 weeks, is not judged again. Edit `BRIEF.md` any time:
  the Skeptic reads it every week.

## Follow-up check
Does a signal keep growing after we judged it? Each ledger line now stores the candidate games' review counts
at judging time. `python -m radar.followup` (run inside the weekly digest) revisits every line 28 and 56 days
later and writes `followup.jsonl` next to the ledger: a game "held" if its last-7-day reviews are at least half
of what they were and its total grew, else "faded"; a line is held if any game held. The digest's "Follow-up"
section lists new results and a running tally (held share for keeps vs kills, and per failing check for kills),
so you can see whether the Skeptic kills good ideas. Runs weekly, no API calls, free.

## Monthly market map
- **monthly** (1st of the month, 07:17 UTC): `python -m radar.market_map` ranks tag pairs from the database:
  rising (more releases than the 3 months before, weighted by how often those games became hits), saturating
  (growing but already crowded), and small-team odds (how often tiny-team games in the pair reached 500+
  reviews). One Opus call writes a one-page read for your brief; every number in it is checked against the
  tables and a bullet with an unbacked number is dropped. Lands in the data repo as `market/YYYY-MM.md`
  (log in `logs/`), with a phone push. Cost about $1-3 a month from the same monthly cap as the digest; tick
  `no_llm` on a manual run for tables only. An API error still writes the tables, with a banner, and fails the job.

## Cost guards
- Prepaid Anthropic credit with auto-reload off: a hard ceiling.
- Console monthly spend limit: set it to at least $60, above the code's cap so the code stops first.
- The code's own caps in `radar/config.py`: $50 per month and $12 per run. Before every call the
  worst case (input estimate plus the full output limit; for a research call a 200k-token input and every web
  search at $0.01) is added to the `spend` table's total; if it would
  pass a cap the call is not made. After each call the real cost (tokens plus web searches) is written to `spend`.
- GitHub Actions spending limit of $0.

Target: under $50 a month, all LLM stages included.

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
