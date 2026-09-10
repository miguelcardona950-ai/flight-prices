# Weekly flight prices

A single Python script that prices a short list of routes once a week and
commits the results to `reports/<YYYY-MM-DD>.md`.

No framework, no dependencies — standard library only.

Needs **Python 3.11 or newer** for `tomllib`, the stdlib TOML parser. The
GitHub Actions workflow pins 3.12, so CI is fine regardless of what's on your
laptop. If your local `python3` is older, the script says so and you can either
use a newer interpreter or `pip install tomli`, which it falls back to.

## Files

| File | Purpose |
| --- | --- |
| `check_flights.py` | The whole program. |
| `routes.toml` | The routes and search settings you edit. |
| `reports/` | One dated markdown file per run. |
| `.github/workflows/flight-prices.yml` | Weekly schedule + commit step. |

## Setup

**1. Get an API key.**
The script ships with an adapter for [SerpApi](https://serpapi.com)'s Google
Flights engine, which authenticates with a single key. Sign up and copy the key
from your dashboard. (See *Using a different provider* below to swap it out.)

**2. Add the key as a repository secret.**
In the repo: **Settings → Secrets and variables → Actions → New repository
secret**. Name it exactly `FLIGHT_API_KEY` and paste the key as the value.

**3. Edit `routes.toml`.**
Replace the sample routes with yours. Airport codes are 3-character IATA codes
(`SFO`, `LHR`, `NRT`). Anything under `[settings]` is a default that a single
route can override:

```toml
[settings]
depart_in_days = 30      # how far ahead to price
return_after_days = 7    # trip length; 0 means one way
currency = "USD"
adults = 1

[[routes]]
name = "San Francisco → Lisbon"
origin = "SFO"
destination = "LIS"

[[routes]]
origin = "SFO"
destination = "HND"
return_after_days = 14   # override just for this route
```

**4. Push, then confirm the schedule.**
The workflow runs Mondays at 13:00 UTC. Change the `cron:` line in
`.github/workflows/flight-prices.yml` to move it. Use the **Actions** tab →
*Weekly flight prices* → **Run workflow** to trigger a run by hand and check
the setup before waiting a week.

The workflow needs permission to push its commits. If the run fails at the
commit step, go to **Settings → Actions → General → Workflow permissions** and
select **Read and write permissions**.

## Running it locally

```bash
FLIGHT_API_KEY=your-key-here python3 check_flights.py
```

It prints each route as it goes and writes `reports/<today>.md`. Re-running on
the same day overwrites that day's file.

## What happens when something breaks

The script treats a partial report as worse than no report, so it prices every
route into memory first and only writes the file once all of them succeeded.

- **Missing `FLIGHT_API_KEY`, or a broken `routes.toml`** — fails before any
  network call.
- **HTTP 401/403** — fails immediately with a message pointing at the key. No
  retries, so a bad key doesn't burn through your quota.
- **Timeouts, network errors, HTTP 429/5xx** — retried 3 times with a growing
  delay, then the run fails.
- **A route with no flights** — fails the run by default. If one of your routes
  is genuinely sparse and you would rather see it reported as empty, set
  `fail_on_empty = false` in `[settings]`.

Every failure exits non-zero with the reason on stderr, which fails the
workflow step. The commit step never runs, so nothing lands in `reports/`.
GitHub emails you when a scheduled workflow fails.

The file itself is written atomically (rendered in memory, written to a temp
file, then moved into place), so a cancelled job can't leave a truncated report
behind either.

## Using a different provider

Provider-specific code is confined to one block in `check_flights.py`, marked
`# Provider adapter`. To switch, rewrite two functions:

- `search_route()` — build the request and call `_get_json()`.
- `_parse_offers()` — turn the response into a list of `Offer` records.

Also update `PROVIDER_NAME` and `API_URL`. Nothing else in the script knows or
cares where the numbers came from. If your provider authenticates with a header
instead of a query parameter, pass it through the `headers` dict in
`_get_json()`.

## Notes

- **Prices are indicative.** The report tracks the trend for a fixed trip shape
  (`depart_in_days` ahead, `return_after_days` long), recalculated each run. It
  is not a booking quote.
- **GitHub pauses idle schedules.** A repository with no commits for 60 days
  has its scheduled workflows disabled. Since this workflow commits a report
  every week, it keeps itself alive — but if you turn off the commit step, the
  schedule will eventually stop.
- **API quota.** Each run makes one request per route. Three routes on a weekly
  schedule is about 13 requests a month.
- **Dates are UTC.** The filename and the search dates come from the UTC clock,
  so CI and a local run on the same day agree. If you run it late in the evening
  in a Western timezone, the report is dated tomorrow.
