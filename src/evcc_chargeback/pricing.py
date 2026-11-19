"""Price sessions with an agreed fixed tariff per kWh that changes on given dates."""

import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CENT = Decimal("0.01")
MAX_DECIMALS = 5
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
PRICE_PATTERN = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)")


@dataclass(frozen=True)
class Tariff:
    start: date  # first day on which the price applies
    price: Decimal  # currency per kWh


def _parse_date(text: str, entry: str) -> date:
    try:
        if not DATE_PATTERN.fullmatch(text):
            raise ValueError
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"bad date {text!r} in tariff entry {entry!r}; use YYYY-MM-DD") from None


def _parse_price(text: str, entry: str) -> Decimal:
    if "," in text:
        raise ValueError(f"bad price {text!r} in tariff entry {entry!r}; use a decimal point, not a comma")
    try:
        if not PRICE_PATTERN.fullmatch(text):
            raise InvalidOperation
        price = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"bad price {text!r} in tariff entry {entry!r}") from None
    if not price.is_finite():
        raise ValueError(f"bad price {text!r} in tariff entry {entry!r}")
    if price < 0:
        raise ValueError(f"negative price {text!r} in tariff entry {entry!r}")
    if price.normalize().as_tuple().exponent < -MAX_DECIMALS:
        raise ValueError(f"price {text!r} in tariff entry {entry!r} has more than {MAX_DECIMALS} decimal places")
    return price


def parse_tariffs(text: str) -> list[Tariff]:
    """Parse "2027-01-01:0.31,2028-01-01:0.29" into tariffs sorted by start date."""
    tariffs: dict[date, Tariff] = {}
    for entry in (part.strip() for part in text.split(",")):
        if not entry:
            continue
        day_text, separator, price_text = entry.partition(":")
        if not separator:
            if entry.isdigit():
                raise ValueError(
                    f"bad tariff entry {entry!r}; a price takes a decimal point, not a comma (comma separates entries)"
                )
            raise ValueError(f"bad tariff entry {entry!r}; use YYYY-MM-DD:price")
        start = _parse_date(day_text.strip(), entry)
        if start in tariffs:
            raise ValueError(f"duplicate start date {start.isoformat()} in tariff entry {entry!r}")
        tariffs[start] = Tariff(start, _parse_price(price_text.strip(), entry))
    if not tariffs:
        raise ValueError("no tariff entries; use YYYY-MM-DD:price[,YYYY-MM-DD:price]")
    return sorted(tariffs.values(), key=lambda t: t.start)


def tariff_for(tariffs: list[Tariff], day: date) -> Tariff:
    """Return the tariff with the latest start date on or before `day`."""
    if not tariffs:
        raise ValueError("no tariffs defined")
    applicable = [t for t in tariffs if t.start <= day]
    if not applicable:
        first = min(t.start for t in tariffs)
        raise ValueError(f"no tariff covers {day.isoformat()}; the first tariff starts on {first.isoformat()}")
    return max(applicable, key=lambda t: t.start)


def amount(energy_kwh: Decimal, price_per_kwh: Decimal) -> Decimal:
    """Energy times price, rounded half up to cents."""
    return (energy_kwh * price_per_kwh).quantize(CENT, ROUND_HALF_UP)
