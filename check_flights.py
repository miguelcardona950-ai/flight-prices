#!/usr/bin/env python3
"""Weekly flight-price check.

Reads a small list of routes from routes.toml, prices each one against a
flight-search API, and writes two files: reports/<YYYY-MM-DD>.md to read, and
data.json, the append-only price history that index.html charts.

One rule shapes the whole script: nothing is written unless *every* route was
priced successfully. Any failure - missing key, bad config, HTTP error,
unparseable response, or a route with no offers - aborts the run with a
non-zero exit code and leaves reports/ and data.json exactly as they were. A
partial report is worse than no report because it looks complete, and a
half-merged history is worse still: it is the record.

Standard library only (Python 3.11+); nothing to pip install.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import tomllib                       # Python 3.11+
except ModuleNotFoundError:              # pragma: no cover - older interpreters
    try:
        import tomli as tomllib          # pip install tomli
    except ModuleNotFoundError:
        sys.exit(
            "ERROR: reading routes.toml needs tomllib, added in Python 3.11 "
            f"(this is {sys.version_info.major}.{sys.version_info.minor}).\n"
            "Run the script with a newer Python, or `pip install tomli`.\n"
            "No report was written."
        )

API_KEY_ENV = "FLIGHT_API_KEY"

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "routes.toml"
REPORTS_DIR = ROOT / "reports"
HISTORY_PATH = ROOT / "data.json"

# Bumped only when the shape of data.json changes incompatibly. index.html
# checks it too, so an old page refuses new data rather than mis-drawing it.
HISTORY_SCHEMA = 1

# An actor run is a container cold start plus live scraping, so it is slow by
# nature and each attempt is billed. Hence the long timeout and few retries.
REQUEST_TIMEOUT = 180       # seconds allowed per actor run
MAX_ATTEMPTS = 2            # attempts per route; each one starts a BILLED run
BACKOFF_SECONDS = 15        # multiplied by the attempt number
RETRY_STATUSES = {429, 500, 502, 503, 504}

DEFAULT_SETTINGS = {
    "depart_in_days": 30,
    "return_after_days": 7,
    "currency": "USD",
    "adults": 1,
    "fail_on_empty": True,
    "max_offers_per_route": 3,
}


class FlightCheckError(Exception):
    """Anything that should abort the run before a report is written."""


# ---------------------------------------------------------------------------
# Provider adapter
#
# Everything specific to the flight-search API lives in this block. To move to
# a different provider, rewrite search_route() and _parse_offers(); the rest of
# the script does not care where the numbers came from.
#
# Current provider: the kuezi/flight-offers-api actor on Apify
# (https://apify.com/kuezi/flight-offers-api), called through Apify's
# run-sync-get-dataset-items endpoint. The actor stands in for the retired
# Amadeus Self-Service flight-offers call and returns Amadeus-shaped offers,
# so the parsing below follows Amadeus field names.
#
# FLIGHT_API_KEY holds an Apify API token. It travels in an Authorization
# header, never in the query string.
# ---------------------------------------------------------------------------

PROVIDER_NAME = "Amadeus-shaped offers via Apify (kuezi/flight-offers-api)"
API_URL = (
    "https://api.apify.com/v2/acts/kuezi~flight-offers-api"
    "/run-sync-get-dataset-items"
)
USER_AGENT = "weekly-flight-prices/2.0"

# How many offers to ask the actor for per route. The actor bills per offer
# saved, so keep this just deep enough that the cheapest fare in the list
# really is the cheapest.
API_MAX_OFFERS = 10


@dataclass(frozen=True)
class Offer:
    price: float
    currency: str
    airlines: str
    stops: int
    duration_minutes: int | None


def search_route(route: dict, api_key: str, today: date) -> list[Offer]:
    """Return every offer the actor found for this route, cheapest first."""
    outbound, inbound = trip_dates(route, today)

    actor_input = {
        "endpoint": "flight-offers",
        "originLocationCode": route["origin"],
        "destinationLocationCode": route["destination"],
        "departureDate": outbound.isoformat(),
        "adults": int(route["adults"]),
        "currencyCode": route["currency"],
        "max": max(API_MAX_OFFERS, int(route["max_offers_per_route"])),
        "oneWay": inbound is None,
    }
    if inbound:
        # The actor searches one-way unless told otherwise, so a round trip
        # needs the return date and oneWay=False together.
        actor_input["returnDate"] = inbound.isoformat()

    label = route_label(route)
    payload = _request_json(API_URL, label, body=actor_input, token=api_key)

    # run-sync-get-dataset-items answers with the dataset rows themselves: a
    # bare list of flight offers, the equivalent of Amadeus's response.data.
    # Apify reports its own failures as a JSON object instead, so HTTP 200 is
    # still not proof of success.
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            raise FlightCheckError(
                f"{label}: Apify reported {error.get('type') or 'an error'}: "
                f"{error.get('message') or error}"
            )
        raise FlightCheckError(
            f"{label}: expected a list of offers from the actor, got a JSON "
            f"object with keys {sorted(payload)[:6]}."
        )
    if not isinstance(payload, list):
        raise FlightCheckError(
            f"{label}: expected a list of offers from the actor, got "
            f"{type(payload).__name__}. The actor's output shape may have changed."
        )

    return _parse_offers(payload, route["currency"])


def _parse_offers(rows: list, currency: str) -> list[Offer]:
    """Turn Amadeus-shaped flight offers into Offer records, cheapest first."""
    offers: list[Offer] = []

    for row in rows:
        if not isinstance(row, dict):
            continue

        price = row.get("price") or {}
        # Amadeus sends money as strings, and grandTotal is the one with fees.
        try:
            amount = float(price.get("grandTotal") or price.get("total"))
        except (TypeError, ValueError):
            continue

        itineraries = [it for it in (row.get("itineraries") or []) if isinstance(it, dict)]
        # itineraries[0] is the outbound leg. The Depart, Stops and Duration
        # columns all describe that leg; the price covers the whole trip.
        outbound = itineraries[0] if itineraries else {}
        segments = [s for s in (outbound.get("segments") or []) if isinstance(s, dict)]

        offers.append(
            Offer(
                price=amount,
                currency=price.get("currency") or currency,
                airlines=_airlines(row, segments),
                stops=max(len(segments) - 1, 0),
                duration_minutes=_iso8601_minutes(outbound.get("duration")),
            )
        )

    offers.sort(key=lambda offer: offer.price)
    return offers


def _airlines(row: dict, segments: list) -> str:
    """Airline names when the actor supplies them, otherwise IATA codes.

    The dataset rows are Amadeus's response.data, which carries carrier codes
    but not the dictionaries.carriers name lookup, so this normally renders as
    codes like "TP". Segment-level name fields are tried first in case the
    actor's googleFlights metadata fills one in.
    """
    for key in ("carrierName", "airline", "airlineName"):
        names = {seg[key] for seg in segments if isinstance(seg.get(key), str) and seg[key]}
        if names:
            return " / ".join(sorted(names))

    codes = {c for c in (row.get("validatingAirlineCodes") or []) if isinstance(c, str) and c}
    if not codes:
        codes = {
            seg["carrierCode"]
            for seg in segments
            if isinstance(seg.get("carrierCode"), str) and seg["carrierCode"]
        }
    return " / ".join(sorted(codes)) or "unknown"


def _iso8601_minutes(value: object) -> int | None:
    """Minutes from an ISO-8601 duration such as PT14H30M or P1DT2H5M."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(
        r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:\d+(?:[.,]\d+)?S)?)?",
        value.strip(),
    )
    if not match:
        return None
    days, hours, minutes = (int(part) if part else 0 for part in match.groups())
    return (days * 1440 + hours * 60 + minutes) or None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _timeout_error(label: str) -> "FlightCheckError":
    """A timeout is not retried, so explain why and what to check."""
    return FlightCheckError(
        f"{label}: no response within {REQUEST_TIMEOUT}s. The actor run is "
        "probably still going on Apify and will be billed either way, so it was "
        "not retried. Check the run in the Apify console, and raise "
        "REQUEST_TIMEOUT if these searches are simply slow."
    )


def _request_json(
    url: str,
    label: str,
    body: dict | None = None,
    token: str = "",
) -> dict | list:
    """Send a request and return the decoded JSON, retrying transient failures.

    POSTs when `body` is given, otherwise GETs. The token travels in an
    Authorization header, so it never reaches a URL, a log line or an error
    message - report the route instead.

    Timeouts are deliberately *not* retried: the actor run is probably still
    executing on Apify's side and will be billed, so a second attempt would pay
    twice for the same search. Rate limits and 5xx still retry.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"

    last_error = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))

        except urllib.error.HTTPError as exc:
            detail = _error_body(exc)
            if exc.code in (401, 403):
                raise FlightCheckError(
                    f"{label}: Apify rejected the credentials (HTTP {exc.code}). "
                    f"Check that {API_KEY_ENV} holds a valid Apify API token."
                    f"{detail}"
                ) from exc
            if exc.code not in RETRY_STATUSES:
                raise FlightCheckError(
                    f"{label}: Apify returned HTTP {exc.code}.{detail}"
                ) from exc
            last_error = f"HTTP {exc.code}{detail}"

        except TimeoutError as exc:
            raise _timeout_error(label) from exc

        except urllib.error.URLError as exc:
            # A connect-phase timeout arrives wrapped in URLError.
            if isinstance(exc.reason, TimeoutError):
                raise _timeout_error(label) from exc
            last_error = f"network error: {exc.reason}"

        except json.JSONDecodeError as exc:
            last_error = f"response was not JSON ({exc})"

        if attempt < MAX_ATTEMPTS:
            delay = BACKOFF_SECONDS * attempt
            print(
                f"  {label}: attempt {attempt}/{MAX_ATTEMPTS} failed "
                f"({last_error}); retrying in {delay}s",
                file=sys.stderr,
            )
            time.sleep(delay)

    raise FlightCheckError(
        f"{label}: gave up after {MAX_ATTEMPTS} attempts. Last error: {last_error}"
    )


def _error_body(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", "replace").strip()
    except Exception:
        return ""
    return f" Response: {body[:300]}" if body else ""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def require_api_key() -> str:
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key:
        raise FlightCheckError(
            f"{API_KEY_ENV} is not set. Export it locally, or add it as a "
            f"repository secret named {API_KEY_ENV} for the GitHub Actions run."
        )
    return key


def load_config(path: Path) -> list[dict]:
    """Parse and fully validate routes.toml before any network call happens."""
    if not path.exists():
        raise FlightCheckError(f"Config file not found: {path}")

    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise FlightCheckError(f"{path.name} is not valid TOML: {exc}") from exc

    provided = raw.get("settings", {})
    unknown = set(provided) - set(DEFAULT_SETTINGS)
    if unknown:
        raise FlightCheckError(
            f"{path.name}: unknown key(s) in [settings]: {', '.join(sorted(unknown))}"
        )
    settings = {**DEFAULT_SETTINGS, **provided}

    entries = raw.get("routes") or []
    if not entries:
        raise FlightCheckError(f"{path.name}: no [[routes]] defined - nothing to price.")

    routes: list[dict] = []
    for index, entry in enumerate(entries, start=1):
        route = {**settings, **entry}

        for field in ("origin", "destination"):
            code = str(route.get(field, "")).strip().upper()
            if len(code) != 3 or not code.isalnum():
                raise FlightCheckError(
                    f"{path.name}: route #{index} has an invalid {field} "
                    f"({entry.get(field)!r}); expected a 3-character airport code."
                )
            route[field] = code

        if route["origin"] == route["destination"]:
            raise FlightCheckError(
                f"{path.name}: route #{index} flies {route['origin']} to itself."
            )
        if int(route["depart_in_days"]) < 1:
            raise FlightCheckError(
                f"{path.name}: route #{index} has depart_in_days < 1; "
                "the report prices future trips only."
            )
        if int(route["return_after_days"]) < 0:
            raise FlightCheckError(
                f"{path.name}: route #{index} has a negative return_after_days "
                "(use 0 for a one-way search)."
            )
        if int(route["adults"]) < 1:
            raise FlightCheckError(f"{path.name}: route #{index} has adults < 1.")

        route.setdefault("name", f"{route['origin']} → {route['destination']}")
        routes.append(route)

    # data.json keys each route by origin-destination, so two routes sharing a
    # city pair would overwrite each other's history. Refuse rather than
    # silently merge two different trips into one series.
    seen: dict[str, int] = {}
    for index, route in enumerate(routes, start=1):
        key = history_key(route)
        if key in seen:
            raise FlightCheckError(
                f"{path.name}: routes #{seen[key]} and #{index} are both "
                f"{key}. The price history keys routes by city pair, so they "
                "cannot be told apart. Drop one, or point it at a nearby "
                "airport."
            )
        seen[key] = index

    return routes


def trip_dates(route: dict, today: date) -> tuple[date, date | None]:
    outbound = today + timedelta(days=int(route["depart_in_days"]))
    nights = int(route["return_after_days"])
    return outbound, (outbound + timedelta(days=nights) if nights > 0 else None)


def route_label(route: dict) -> str:
    return f"{route['origin']}->{route['destination']}"


def history_key(route: dict) -> str:
    """Stable identity for a route in data.json.

    Origin and destination are the only parts of a route that survive config
    edits: `name` is free text, and the trip-length settings get tweaked.
    """
    return f"{route['origin']}-{route['destination']}"


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def render_report(
    run_date: date,
    generated_at: datetime,
    results: list[tuple[dict, list[Offer]]],
) -> str:
    count = len(results)
    lines = [
        f"# Flight prices — {run_date.isoformat()}",
        "",
        f"Generated {generated_at:%Y-%m-%d %H:%M} UTC · "
        f"{count} route{'' if count == 1 else 's'} · source: {PROVIDER_NAME}",
        "",
        "| Route | Depart | Return | Best price | Airline | Stops | Duration |",
        "| --- | --- | --- | ---: | --- | ---: | --- |",
    ]

    for route, offers in results:
        outbound, inbound = trip_dates(route, run_date)
        best = offers[0] if offers else None
        lines.append(
            "| {name} | {depart} | {ret} | {price} | {airline} | {stops} | {duration} |".format(
                name=_cell(route["name"]),
                depart=outbound.isoformat(),
                ret=inbound.isoformat() if inbound else "one way",
                price=_price(best),
                airline=_cell(best.airlines) if best else "—",
                stops=best.stops if best else "—",
                duration=_duration(best.duration_minutes) if best else "—",
            )
        )

    for route, offers in results:
        outbound, inbound = trip_dates(route, run_date)
        shown = offers[: int(route["max_offers_per_route"])]
        adults = int(route["adults"])

        lines += [
            "",
            f"## {route['name']}",
            "",
            f"{route['origin']} → {route['destination']} · depart {outbound.isoformat()} · "
            + (f"return {inbound.isoformat()}" if inbound else "one way")
            + f" · {adults} adult{'' if adults == 1 else 's'}",
            "",
        ]

        if not shown:
            lines.append("No offers returned for this search.")
            continue

        lines.append(f"Cheapest {len(shown)} of {len(offers)} offers:")
        lines.append("")
        for rank, offer in enumerate(shown, start=1):
            lines.append(
                f"{rank}. **{_price(offer)}** — {offer.airlines}, "
                f"{_stops(offer.stops)}, {_duration(offer.duration_minutes)}"
            )

    return "\n".join(lines) + "\n"


def _cell(value: object) -> str:
    """Keep stray pipes from breaking the markdown table."""
    return str(value).replace("|", "\\|")


def _price(offer: Offer | None) -> str:
    return f"{offer.price:,.0f} {offer.currency}" if offer else "—"


def _stops(stops: int) -> str:
    if stops == 0:
        return "direct"
    return f"{stops} stop{'' if stops == 1 else 's'}"


def _duration(minutes: int | None) -> str:
    if not minutes:
        return "—"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def write_text_atomic(path: Path, content: str) -> None:
    """Write a file in one atomic step.

    Content is built entirely in memory first, then the file lands via
    os.replace(), so a crash or a cancelled CI job can never leave a
    half-written file behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(content, encoding="utf-8")
    os.replace(temp, path)


# ---------------------------------------------------------------------------
# Price history
# ---------------------------------------------------------------------------


def load_history(path: Path) -> dict:
    """Read data.json, or return an empty history if it does not exist yet.

    Called before the first network request, so a damaged history file costs
    nothing to discover. A file that exists but cannot be understood is an
    error and never a reason to start over: it is the only copy of the record.
    """
    if not path.exists():
        return {
            "schema": HISTORY_SCHEMA,
            "updated": None,
            "routes": {},
            "history": {},
        }

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FlightCheckError(
            f"{path.name} is not valid JSON ({exc}). It holds the entire price "
            "history, so it was left untouched. Repair or delete it by hand."
        ) from exc
    except OSError as exc:
        raise FlightCheckError(f"Could not read {path.name}: {exc}") from exc

    if not isinstance(raw, dict):
        raise FlightCheckError(
            f"{path.name} should hold a JSON object, found "
            f"{type(raw).__name__}. It was left untouched."
        )

    if raw.get("schema") != HISTORY_SCHEMA:
        raise FlightCheckError(
            f"{path.name} is schema {raw.get('schema')!r} but this script "
            f"writes schema {HISTORY_SCHEMA}. It was left untouched."
        )

    for field in ("routes", "history"):
        if not isinstance(raw.get(field), dict):
            raise FlightCheckError(
                f'{path.name}: "{field}" should be a JSON object. '
                "It was left untouched."
            )

    return raw


def merge_run(
    history: dict,
    run_date: date,
    generated_at: datetime,
    results: list[tuple[dict, list[Offer]]],
) -> dict:
    """Fold this run's cheapest fares into the history, in memory.

    The whole day's block is replaced rather than added to, so re-running on
    the same date corrects that day instead of appending a duplicate.
    """
    day: dict[str, dict] = {}

    for route, offers in results:
        key = history_key(route)
        outbound, inbound = trip_dates(route, run_date)
        best = offers[0] if offers else None

        history["routes"][key] = {
            "name": route["name"],
            "origin": route["origin"],
            "destination": route["destination"],
        }
        # A route with no offers is recorded with a null price rather than
        # dropped, so the history says "looked, found nothing" and the chart
        # can leave a gap.
        day[key] = {
            "price": round(best.price, 2) if best else None,
            "currency": best.currency if best else route["currency"],
            "airlines": best.airlines if best else None,
            "stops": best.stops if best else None,
            "duration_minutes": best.duration_minutes if best else None,
            "depart": outbound.isoformat(),
            "return": inbound.isoformat() if inbound else None,
            "offers": len(offers),
        }

    history["schema"] = HISTORY_SCHEMA
    history["updated"] = generated_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    history["history"][run_date.isoformat()] = day
    return history


def render_history(history: dict) -> str:
    """Serialise the history with stable key order, for small readable diffs."""
    ordered = {
        "schema": history["schema"],
        "updated": history["updated"],
        "routes": {key: history["routes"][key] for key in sorted(history["routes"])},
        "history": {
            day: {
                key: history["history"][day][key]
                for key in sorted(history["history"][day])
            }
            for day in sorted(history["history"])
        },
    }
    return json.dumps(ordered, indent=2, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        api_key = require_api_key()
        routes = load_config(CONFIG_PATH)
        # Validated before a single billed API call, so a damaged history file
        # is found for free.
        history = load_history(HISTORY_PATH)

        now = datetime.now(timezone.utc)
        today = now.date()

        # Price every route before writing anything. If any route raises, the
        # run ends here and reports/ is left exactly as it was.
        results: list[tuple[dict, list[Offer]]] = []
        for route in routes:
            label = route_label(route)
            print(f"Pricing {label} ...")
            offers = search_route(route, api_key, today)

            if not offers and route["fail_on_empty"]:
                raise FlightCheckError(
                    f"{label}: the API returned no offers. Check the airport codes "
                    "and travel dates, or set fail_on_empty = false in routes.toml "
                    "to report empty routes instead of failing the run."
                )

            best = f"{offers[0].price:,.0f} {offers[0].currency}" if offers else "no offers"
            print(f"  {len(offers)} offers, best {best}")
            results.append((route, offers))

        # Build both outputs completely before either one is written. Two
        # files cannot be made atomic together, but by this point nothing is
        # left that can fail for any reason other than disk I/O, and both
        # files are idempotent per day, so a re-run repairs a split write.
        report = render_report(today, now, results)
        history_json = render_history(merge_run(history, today, now, results))

        report_path = REPORTS_DIR / f"{today.isoformat()}.md"
        write_text_atomic(HISTORY_PATH, history_json)
        write_text_atomic(report_path, report)

        runs = len(history["history"])
        print(
            f"\nWrote reports/{report_path.name} and {HISTORY_PATH.name} "
            f"({len(results)} routes; {runs} run{'' if runs == 1 else 's'} "
            "on record)."
        )
        return 0

    except FlightCheckError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        print(
            "Nothing was written; the report and data.json are unchanged.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
