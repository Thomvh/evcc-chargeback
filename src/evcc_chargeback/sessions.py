"""Read charging sessions from the evcc API and select the sessions of one vehicle and month."""

import json
import re
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from decimal import ROUND_HALF_UP, Decimal

from .pricing import Tariff, amount, tariff_for

CENT = Decimal("0.01")
KWH = Decimal("0.001")


@dataclass(frozen=True)
class Session:
    id: int
    start: datetime
    end: datetime
    loadpoint: str
    vehicle: str
    identifier: str
    energy_kwh: Decimal
    meter_start: Decimal | None
    meter_stop: Decimal | None
    price_per_kwh: Decimal  # the agreed tariff, or the average that evcc recorded
    cost: Decimal  # the amount on the statement
    evcc_cost: Decimal | None  # what evcc recorded, kept for the record


def parse_time(value: str | None) -> datetime | None:
    """Parse an evcc timestamp. evcc writes nanoseconds; Python accepts at most microseconds."""
    if not value or value.startswith("0001-01-01"):
        return None
    return datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", value))


def _get(evcc_url: str, path: str, timeout: float = 30):
    """GET from the evcc API; this changes nothing in evcc."""
    with urllib.request.urlopen(f"{evcc_url.rstrip('/')}/api/{path}", timeout=timeout) as response:
        data = json.load(response)
    # Older evcc versions wrap the payload in {"result": ...}.
    return data["result"] if isinstance(data, dict) and "result" in data else data


def fetch_sessions(evcc_url: str, year: int, month: int | None = None) -> list[dict]:
    """Read the finished sessions of a month, or of a whole year. evcc stores a session when it ends."""
    return _get(evcc_url, f"sessions?year={year}" + (f"&month={month}" if month else ""))


def fetch_period(evcc_url: str, period: date) -> list[dict]:
    """Read the month with the month before and after it.

    evcc selects a month in the time zone of its own host. When that differs from the billing time zone, a
    session near the month boundary is in the neighbour month.
    """
    before, after = period - timedelta(days=1), period + timedelta(days=32)
    found = {}
    for month in (before, period, after):
        for item in fetch_sessions(evcc_url, month.year, month.month):
            found[item["id"]] = item
    return list(found.values())


def fetch_year(evcc_url: str, year: int) -> list[dict]:
    """Read the year with December before and January after it (see `fetch_period`)."""
    found = {}
    for part in (fetch_sessions(evcc_url, year - 1, 12), fetch_sessions(evcc_url, year), fetch_sessions(evcc_url, year + 1, 1)):
        for item in part:
            found[item["id"]] = item
    return list(found.values())


def open_sessions(evcc_url: str, period: date, vehicles: list[str], zone: ZoneInfo, now: datetime) -> list[str]:
    """Return the loadpoints where a car has been connected since before the end of the month.

    Its session is not in the session list until the car disconnects, so the month is not complete yet.
    """
    month_end = datetime(*(period + timedelta(days=32)).replace(day=1).timetuple()[:3], tzinfo=zone)
    waiting = []
    for loadpoint in _get(evcc_url, "state").get("loadpoints", []):
        if not loadpoint.get("connected") or (loadpoint.get("vehicleTitle") or vehicles[0]) not in vehicles:
            continue
        duration = loadpoint.get("connectedDuration")
        # Without a duration the start is unknown; that also keeps the month open.
        if duration is None or now.astimezone(timezone.utc) - timedelta(seconds=float(duration)) < month_end:
            waiting.append(loadpoint.get("title") or "?")
    return waiting


def _decimal(value, unit: Decimal) -> Decimal | None:
    return None if value is None or value == "" else Decimal(str(value)).quantize(unit, ROUND_HALF_UP)


def _local(value: str | None, zone: ZoneInfo) -> datetime | None:
    time = parse_time(value)
    return time.astimezone(zone) if time else None


def in_period(start: datetime | None, period: date) -> bool:
    """A session belongs to the month in which it starts, in the billing time zone."""
    return start is not None and (start.year, start.month) == (period.year, period.month)


def doubtful_sessions(raw: list[dict], vehicles: list[str], identifiers: list[str], period: date,
                      zone: ZoneInfo) -> list[int]:
    """Return the ids of the sessions of the month of which the vehicle is in doubt.

    A session without a vehicle can be anybody's. A session that was started with one of the car's own RFID
    tags (`identifiers`) but has another vehicle is a wrong selection in evcc.
    """
    tags = {tag.lower() for tag in identifiers}
    return sorted(
        item["id"] for item in raw if in_period(_local(item.get("created"), zone), period)
        and (not item.get("vehicle")
             or ((item.get("identifier") or "").lower() in tags and item["vehicle"] not in vehicles))
    )


def select_sessions(raw: list[dict], vehicles: list[str], period: date, tariffs: list[Tariff] | None = None,
                    zone: ZoneInfo = ZoneInfo("Europe/Amsterdam")) -> list[Session]:
    """Return the sessions of the vehicle that started in the period's month, oldest first.

    `vehicles` holds the evcc vehicle title and its earlier titles: evcc stores the title in the session, so
    a renamed vehicle has sessions under both. With `tariffs` the amount is energy times the agreed tariff of
    the start day; without, it is the cost that evcc recorded. A session without an end time is still
    running; the statement cannot be final, so this raises an error.
    """
    selected = []
    for item in raw:
        start = _local(item.get("created"), zone)
        if item.get("vehicle") not in vehicles or not in_period(start, period):
            continue
        end = _local(item.get("finished"), zone)
        if end is None:
            raise ValueError(f"session {item['id']} is still running; run again when it has finished")
        energy = _decimal(item.get("chargedEnergy"), KWH) or Decimal("0.000")
        evcc_cost = _decimal(item.get("price"), CENT)
        if tariffs is not None:
            price_per_kwh = tariff_for(tariffs, start.date()).price
            cost = amount(energy, price_per_kwh)
        elif evcc_cost is None:
            raise ValueError(f"session {item['id']} has no price in evcc; set a grid tariff in evcc")
        else:
            cost = evcc_cost
            price_per_kwh = _decimal(item.get("pricePerKWh"), Decimal("0.0001"))
            if price_per_kwh is None:
                price_per_kwh = (cost / energy).quantize(Decimal("0.0001"), ROUND_HALF_UP) if energy else Decimal(0)
        selected.append(
            Session(
                id=item["id"],
                start=start,
                end=end,
                loadpoint=item.get("loadpoint") or "",
                vehicle=item["vehicle"],
                identifier=item.get("identifier") or "",
                energy_kwh=energy,
                meter_start=_decimal(item.get("meterStart"), KWH),
                meter_stop=_decimal(item.get("meterStop"), KWH),
                price_per_kwh=price_per_kwh,
                cost=cost,
                evcc_cost=evcc_cost,
            )
        )
    return sorted(selected, key=lambda s: (s.start, s.id))


def total_cost(sessions: list[Session]) -> Decimal:
    """Sum of the rounded session costs, so the total matches the lines on the statement."""
    return sum((s.cost for s in sessions), Decimal("0.00"))


def total_energy(sessions: list[Session]) -> Decimal:
    return sum((s.energy_kwh for s in sessions), Decimal("0.000"))
