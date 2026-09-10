#!/usr/bin/env python3
"""Weekly flight-price check.

Reads a small list of routes from routes.toml, prices each one against a
flight-search API, and writes reports/<YYYY-MM-DD>.md.

One rule shapes the whole script: the report is written only when *every*
route was priced successfully. Any failure - missing key, bad config, HTTP
error, unparseable response, or a route with no offers - aborts the run with
a non-zero exit code and leaves reports/ untouched. A partial report is worse
than no report, because it looks complete.

Standard library only (Python 3.11+); nothing to pip install.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
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

REQUEST_TIMEOUT = 30        # seconds allowed per HTTP call
MAX_ATTEMPTS = 3            # attempts per route before giving up
BACKOFF_SECONDS = 5         # multiplied by the attempt number
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
# Current provider: SerpApi's Google Flights engine (https://serpapi.com).
# It authenticates with a single API key passed as a query parameter, which is
# why the key comes from one environment variable.
# ---------------------------------------------------------------------------

PROVIDER_NAME = "Google Flights via SerpApi"
API_URL = "https://serpapi.com/search"
USER_AGENT = "weekly-flight-prices/1.0"


@dataclass(frozen=True)
class Offer:
    price: float
    currency: str
    airlines: str
    stops: int
    duration_minutes: int | None


def search_route(route: dict, api_key: str, today: date) -> list[Offer]:
    """Return every offer the provider knows about, cheapest first."""
    outbound, inbound = trip_dates(route, today)

    params = {
        "engine": "google_flights",
        "departure_id": route["origin"],
        "arrival_id": route["destination"],
        "outbound_date": outbound.isoformat(),
        "type": 1 if inbound else 2,          # 1 = round trip, 2 = one way
        "currency": route["currency"],
        "adults": route["adults"],
        "hl": "en",
        "api_key": api_key,
    }
    if inbound:
        # For a round trip the first response prices the outbound options at
        # the full round-trip fare, which is what we want to track.
        params["return_date"] = inbound.isoformat()

    label = route_label(route)
    payload = _get_json(f"{API_URL}?{urllib.parse.urlencode(params)}", label)

    # The provider answers HTTP 200 with an "error" field for bad keys, unknown
    # airport codes and empty searches, so a 200 is not proof of success.
    if isinstance(payload.get("error"), str):
        raise FlightCheckError(f"{label}: API reported an error: {payload['error']}")

    return _parse_offers(payload, route["currency"])


def _parse_offers(payload: dict, currency: str) -> list[Offer]:
    offers: list[Offer] = []
    groups = (payload.get("best_flights") or []) + (payload.get("other_flights") or [])

    for entry in groups:
        price = entry.get("price")
        if price is None:
            continue
        legs = entry.get("flights") or []
        airlines = sorted({leg["airline"] for leg in legs if leg.get("airline")})
        offers.append(
            Offer(
                price=float(price),
                currency=currency,
                airlines=" / ".join(airlines) or "unknown",
                stops=max(len(legs) - 1, 0),
                duration_minutes=entry.get("total_duration"),
            )
        )

    offers.sort(key=lambda offer: offer.price)
    return offers


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _get_json(url: str, label: str) -> dict:
    """GET a JSON document, retrying transient failures.

    The URL carries the API key, so it is never written to a log line or an
    error message. Report the route instead.
    """
    last_error = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))

        except urllib.error.HTTPError as exc:
            detail = _error_body(exc)
            if exc.code in (401, 403):
                raise FlightCheckError(
                    f"{label}: the API rejected the credentials (HTTP {exc.code}). "
                    f"Check that {API_KEY_ENV} holds a valid key.{detail}"
                ) from exc
            if exc.code not in RETRY_STATUSES:
                raise FlightCheckError(
                    f"{label}: API returned HTTP {exc.code}.{detail}"
                ) from exc
            last_error = f"HTTP {exc.code}{detail}"

        except urllib.error.URLError as exc:
            last_error = f"network error: {exc.reason}"

        except TimeoutError:
            last_error = f"timed out after {REQUEST_TIMEOUT}s"

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

    return routes


def trip_dates(route: dict, today: date) -> tuple[date, date | None]:
    outbound = today + timedelta(days=int(route["depart_in_days"]))
    nights = int(route["return_after_days"])
    return outbound, (outbound + timedelta(days=nights) if nights > 0 else None)


def route_label(route: dict) -> str:
    return f"{route['origin']}->{route['destination']}"


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


def write_report(path: Path, content: str) -> None:
    """Write the report in one atomic step.

    Rendering happens entirely in memory first, then the file lands via
    os.replace(), so a crash or a cancelled CI job can never leave a
    half-written report behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(content, encoding="utf-8")
    os.replace(temp, path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        api_key = require_api_key()
        routes = load_config(CONFIG_PATH)

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

        report_path = REPORTS_DIR / f"{today.isoformat()}.md"
        write_report(report_path, render_report(today, now, results))
        print(f"\nWrote reports/{report_path.name} ({len(results)} routes).")
        return 0

    except FlightCheckError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        print("No report was written.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
