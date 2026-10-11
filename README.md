# animekarma-pmdblist
Builds a PublicMetaDB List based off AnimeKarmaList

Copy `env.example` to `.env` and fill in your API key and a distinct list ID for
each job you want to run on PublicMetaDB or MDBList.

```powershell
uv run -m animelists run --dry-run
uv run -m animelists run
```

The CLI automatically loads the project's `.env`, including job IDs and optional
settings. Existing shell environment variables take precedence. `uv run` installs
the required dependencies in a local virtual environment. Without uv, install
`requirements.txt` and use `python -m animelists run`.

ID lookups are cached in `sync_state.json` across runs: successful mappings for
30 days and unsuccessful mappings (including MDBList lookups returning no
results) for 3 days. Cached misses are logged as `cached miss; lookup skipped`;
they do not make another API request or extend the retry period. This also applies
to dry runs. Use `--force` or `FORCE=1` only when you want to bypass the cache.

Optional Kuroiru filters for `recent`, `popular`, and `trending` can be set in `.env`:

```dotenv
MIN_MEMBERS=10000
MIN_SCORE=7.0
```

Both minimums are inclusive. Leave either setting blank or omit it to disable that
filter. With a filter enabled, titles missing a valid value for that field are
excluded. When both are set, titles must meet both minimums. Popular and trending
are filtered before the `--top` limit; the karma list is unaffected.

Use `--min-members` and `--min-score` to override these settings for one run.
Preview the filtered source lists with `uv run -m animelists run recent popular trending --extract-only`.

For a Linux server, use the airing-aware scheduler to avoid fetching every source
on every cron invocation:

```bash
uv run -m animelists schedule                 # UTC times, reasons, weekly cron previews
uv run -m animelists schedule recent --json   # machine-readable plan for one job
uv run -m animelists tick --dry-run           # show due jobs; no sync or state updates
```

With no job names, it schedules only jobs with list IDs in `.env`. The `recent`
plan applies the same `MIN_MEMBERS` and `MIN_SCORE` filters as the actual list,
including upcoming premieres that qualify. It checks 50 minutes after a listed
broadcast, rounded up to the next 30-minute UTC boundary to batch nearby releases.
By default, list membership matches Kuroiru's own **Recently Released** panel,
then applies your member/score filters. Its current display logic uses a four-day
window with a 50-minute offset after the estimated broadcast; the scheduler waits
at least until the title can enter that panel and also checks when it leaves.
`--hours` is an optional override for a custom window measured from the estimated
broadcast, rather than the default provider panel rules.
These are candidate shows from your filtered source, before TMDB mapping;
planning does not fetch your remote lists or download ID mappings.

Karma scores and popularity can change between broadcasts, so they use separate
cadences: karma every 6 hours, popular every 12 hours, and trending daily at 18:15
UTC. The trending default follows this project's existing guidance to collect the
full daily snapshot after 18:00 UTC; it is not a measured guarantee of provider
refresh time. Recent also gets a daily check at 06:30 UTC to catch feed corrections,
late metadata and newly qualifying titles. Airing times are estimates, and the
recent source still assumes weekly broadcasts when reconstructing the previous
airing. The scheduler uses only the feed's next airing rather than extrapolating
more weekly episodes.

Install dependencies once on the server (`uv sync`, or
`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`), then:

```bash
chmod +x tick.sh
crontab -e
```

Add one cron entry, replacing the path with your checkout's absolute path:

```cron
*/5 * * * * /path/to/animekarma-pmdblist/tick.sh
```

This checks local state every five minutes. Idle ticks make **no HTTP requests**;
the airing plan refreshes once every 24 hours (one `/app` fetch for `recent`), and
only due jobs fetch sources and sync. Refreshing and running jobs in the same tick
share the Kuroiru page. Existing ID and list fingerprint caches continue to avoid
unnecessary mapping and backend calls. All due decisions use UTC timestamps, so
the five-minute cron entry works regardless of the server timezone or DST.
Weekly cron previews become stale as shows move or qualify; the single `tick.sh`
entry keeps adapting without editing your crontab. Remove old cron entries for
the same jobs to avoid overlapping syncs. The legacy GitHub workflow continues
to use its separate Monday schedule.

The first tick syncs every selected job. Later ticks combine missed events into
one catch-up sync per job, lock out overlapping ticks, and retry failed jobs or
plan refreshes after 30 minutes. Successful jobs are recorded separately so one
failure does not repeat all the others. A failed refresh keeps using an unexpired
cached plan; without a valid plan, no jobs run. The existing protection against
replacing a list with zero mapped titles still applies; that job reports failure
and will be retried. Keep both `sync_state.json` and `schedule_state.json` on disk
between cron runs; the latter defaults to the directory containing `STATE_FILE`.
Changing filters, schedule options or list IDs regenerates the plan and syncs the
selected jobs again. Output appends to `run.log`; arrange log rotation on your server.

Tune the tradeoff between freshness and requests with the same options on every tick:

```cron
*/5 * * * * /path/to/animekarma-pmdblist/tick.sh --delay-minutes 60 --batch-minutes 60 --karma-hours 12 --popular-hours 24
```

Other options include `--refresh-hours`, `--retry-minutes`, `--trending-time HH:MM`
(UTC), `--hours`, `--top`, and `--schedule-file`. `PYTHON_BIN` can override the
wrapper's default `.venv/bin/python`. Manual `run` commands bypass scheduling;
avoid running them concurrently with ticks.
