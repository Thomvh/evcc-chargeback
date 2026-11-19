"""Export all charging sessions of a year as charge detail records (CDR) in CSV, independent of the car."""

import csv
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from .sessions import parse_time

KWH = Decimal("0.001")
HEADER = [
    "session_id",
    "charge_point",
    "start",
    "end",
    "energy_kwh",
    "meter_start_kwh",
    "meter_stop_kwh",
    "identifier",
    "vehicle",
]
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class Record:
    id: int
    loadpoint: str
    start: datetime
    end: datetime
    energy_kwh: Decimal
    meter_start: Decimal | None
    meter_stop: Decimal | None
    identifier: str
    vehicle: str


@dataclass(frozen=True)
class Export:
    year: int
    records: list[Record]
    running: list[int]

    @property
    def total_kwh(self) -> Decimal:
        return sum((r.energy_kwh for r in self.records), Decimal("0.000"))


def _kwh(value) -> Decimal | None:
    """Convert an evcc number to kWh with 3 decimals; empty values give None."""
    if value is None or value == "":
        return None
    return Decimal(str(value)).quantize(KWH, ROUND_HALF_UP)


def build_export(raw: list[dict], year: int, loadpoints: list[str] | None = None) -> Export:
    """Select the finished sessions that started in `year` (by the local date in the timestamp's own offset)."""
    records = []
    running = []
    for item in raw:
        start = parse_time(item.get("created"))
        if start is None or start.year != year:
            continue
        if loadpoints and item.get("loadpoint") not in loadpoints:
            continue
        end = parse_time(item.get("finished"))
        if end is None:
            running.append((start, item["id"]))
            continue
        records.append(
            Record(
                id=item["id"],
                loadpoint=item.get("loadpoint") or "",
                start=start,
                end=end,
                energy_kwh=_kwh(item.get("chargedEnergy")) or Decimal("0.000"),
                meter_start=_kwh(item.get("meterStart")),
                meter_stop=_kwh(item.get("meterStop")),
                identifier=item.get("identifier") or "",
                vehicle=item.get("vehicle") or "",
            )
        )
    records.sort(key=lambda r: (r.start, r.id))
    return Export(year=year, records=records, running=[id_ for _, id_ in sorted(running)])


def _text(value: str) -> str:
    """Prefix a text cell with a quote when a spreadsheet would read it as a formula."""
    return "'" + value if value.startswith(FORMULA_PREFIXES) else value


def _number(value: Decimal | None) -> str:
    return "" if value is None else str(value)


def write_csv(export: Export, path: Path) -> Path:
    """Write the records to `path` through a temporary file, so a crash leaves no half file."""
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(HEADER)
            for r in export.records:
                writer.writerow(
                    [
                        r.id,
                        _text(r.loadpoint),
                        r.start.isoformat(timespec="seconds"),
                        r.end.isoformat(timespec="seconds"),
                        _number(r.energy_kwh),
                        _number(r.meter_start),
                        _number(r.meter_stop),
                        _text(r.identifier),
                        _text(r.vehicle),
                    ]
                )
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return path
