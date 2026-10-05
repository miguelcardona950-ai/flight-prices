# Weekly flight prices

A single Python script that prices a short list of routes once a week, commits
the results to `reports/<YYYY-MM-DD>.md`, and appends them to `data.json` — a
running price history that `index.html` charts.

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
| `reports/` | One dated markdown file per run, for reading. |
| `data.json` | The full price history, appended to on every run. |
| `index.html` | A dashboard that charts `data.json`. No build step. |
| `.github/workflows/flight-prices.yml` | Weekly schedule + commit step. |
| `.nojekyll` | Tells GitHub Pages to serve the files as-is. |

`data.json` is created by the first successful run; it is not in the repo until
then.

## Setup

**1. Get an Apify API token.**
The script calls the [kuezi/flight-offers-api](https://apify.com/kuezi/flight-offers-api)
actor on Apify, which stands in for the retired Amadeus Self-Service
flight-offers endpoint and returns Amadeus-shaped offers. Sign up at
[apify.com](https://apify.com), then copy your API token from **Settings →
Integrations** in the Apify Console. (See *Using a different provider* below to
swap it out.)

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
route into memory first and only writes anything once all of them succeeded.
That covers `data.json` too: a failed run leaves the history byte-for-byte
unchanged rather than half-merged.

- **Missing `FLIGHT_API_KEY`, or a broken `routes.toml`** — fails before any
  network call.
- **HTTP 401/403** — fails immediately with a message pointing at the token. No
  retries, so a bad token doesn't burn through your credit.
- **Network errors, HTTP 429/5xx** — retried twice with a growing delay, then
  the run fails. Retries are deliberately few, because each attempt starts a
  *billed* actor run.
- **Timeout** — fails immediately and is **not** retried. An actor run that
  hasn't answered within `REQUEST_TIMEOUT` (180s) is probably still executing on
  Apify's side and will be billed anyway, so retrying would pay twice for the
  same search. If your routes are simply slow, raise `REQUEST_TIMEOUT` rather
  than adding retries.
- **A damaged `data.json`** — unparseable, or a schema this script does not
  write — fails before any API call, and the file is **left untouched**. It is
  the only copy of the history, so it is never silently restarted. Repair or
  delete it by hand.
- **Two routes with the same city pair** — refused at config load. The history
  keys routes by origin-destination, so two `SFO`→`LIS` entries could not be
  told apart.
- **A route with no flights** — fails the run by default. If one of your routes
  is genuinely sparse and you would rather see it reported as empty, set
  `fail_on_empty = false` in `[settings]`.

Every failure exits non-zero with the reason on stderr, which fails the
workflow step. The commit step never runs, so nothing lands in `reports/`.
GitHub emails you when a scheduled workflow fails.

The file itself is written atomically (rendered in memory, written to a temp
file, then moved into place), so a cancelled job can't leave a truncated report
behind either.

## The dashboard

`index.html` reads `data.json` and shows the current price per route, then a
line chart of each route over time. It is plain HTML, CSS and JavaScript in one
file — no build step, no framework, and nothing loaded from a CDN, so whatever
is in the repo is exactly what runs.

**Opening it from disk will not work.** Browsers block `fetch()` on `file://`,
so double-clicking `index.html` cannot read `data.json`; the page says so and
tells you what to do. Serve the folder instead:

```bash
python3 -m http.server 8000
```

Then open <http://localhost:8000>.

**To publish it,** turn on GitHub Pages: **Settings → Pages → Source → Deploy
from a branch**, pick `main` and the `/ (root)` folder. The dashboard lands at
`https://<username>.github.io/<repo>/`, and refreshes itself every time the
weekly job commits. This is a repo setting, so you have to flip it yourself.

Note that Pages serves a **public** site, even from a private repo on some
plans — check before publishing if your routes say something about your travel
plans you would rather keep to yourself.

## Using a different provider

Provider-specific code is confined to one block in `check_flights.py`, marked
`# Provider adapter`. To switch, rewrite two functions:

- `search_route()` — build the request and call `_request_json()`.
- `_parse_offers()` — turn the response into a list of `Offer` records.

Also update `PROVIDER_NAME` and `API_URL`. Nothing else in the script knows or
cares where the numbers came from. `_request_json()` POSTs when given a `body`
and GETs otherwise, and sends the token as `Authorization: Bearer`; adjust the
`headers` dict there if your provider wants it somewhere else.

## Notes

- **Prices are indicative.** The report tracks the trend for a fixed trip shape
  (`depart_in_days` ahead, `return_after_days` long), recalculated each run. It
  is not a booking quote.
- **GitHub pauses idle schedules.** A repository with no commits for 60 days
  has its scheduled workflows disabled. Since this workflow commits a report
  every week, it keeps itself alive — but if you turn off the commit step, the
  schedule will eventually stop.
- **History size.** About 23 KB a year for three weekly routes, so one file
  stays fine indefinitely. Keys are written in sorted order, so each run's
  commit diff is just the lines that changed.
- **Cost.** The actor bills per event: roughly $0.003 per search plus $0.0005
  per offer returned. The script asks for up to 10 offers per route
  (`API_MAX_OFFERS` in the script), so a route costs about $0.008 a run — three
  routes weekly is well under $0.20 a month. Each retry is another billed run,
  which is why there are only two attempts and no retry on timeout.
- **Airline names show as IATA codes** (`TP`, `BA`) rather than full names. The
  actor returns Amadeus's `response.data`, which carries carrier codes but not
  the `dictionaries.carriers` lookup table that maps them to names.
- **Dates are UTC.** The filename and the search dates come from the UTC clock,
  so CI and a local run on the same day agree. If you run it late in the evening
  in a Western timezone, the report is dated tomorrow.
