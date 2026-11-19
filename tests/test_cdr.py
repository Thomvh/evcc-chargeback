import csv
from decimal import Decimal

from evcc_chargeback.cdr import HEADER, build_export, write_csv


def item(id, created, finished="2026-06-01T12:00:00+02:00", loadpoint="Carport", **extra):
    base = {"id": id, "created": created, "finished": finished, "loadpoint": loadpoint, "identifier": "nfc-0123ABCD",
            "vehicle": "Carla", "meterStart": 1555.273, "meterStop": 1561.605, "chargedEnergy": 6.332000000000107}
    return base | extra


def test_year_boundary_uses_start_in_own_offset():
    raw = [
        item(1, "2026-12-31T23:30:00+01:00", "2027-01-01T03:00:00+01:00"),
        item(2, "2027-01-01T00:10:00+01:00", "2027-01-01T03:00:00+01:00"),
        item(3, "2025-12-31T23:59:59+01:00", "2026-01-01T03:00:00+01:00"),
    ]
    assert [r.id for r in build_export(raw, 2026).records] == [1]


def test_running_session_is_listed_not_exported():
    raw = [
        item(1, "2026-03-01T10:00:00+01:00"),
        item(2, "2026-03-02T10:00:00+01:00", "0001-01-01T00:00:00Z"),
        item(3, "2026-03-03T10:00:00+01:00", None),
        item(4, "2027-03-03T10:00:00+01:00", "0001-01-01T00:00:00Z"),
    ]
    export = build_export(raw, 2026)
    assert [r.id for r in export.records] == [1]
    assert export.running == [2, 3]


def test_items_without_start_are_skipped():
    assert build_export([{"id": 1, "finished": "2026-03-01T10:00:00+01:00"}], 2026).records == []


def test_loadpoint_filter():
    raw = [item(1, "2026-03-01T10:00:00+01:00"), item(2, "2026-03-02T10:00:00+01:00", loadpoint="Garage")]
    assert [r.id for r in build_export(raw, 2026, ["Garage"]).records] == [2]
    assert [r.id for r in build_export(raw, 2026, ["garage"]).records] == []
    assert len(build_export(raw, 2026, []).records) == 2
    assert len(build_export(raw, 2026, None).records) == 2


def test_missing_meter_values():
    raw = [
        item(1, "2026-03-01T10:00:00+01:00", meterStart=None, meterStop=None, identifier=None, vehicle=None),
        item(2, "2026-03-02T10:00:00+01:00", meterStart="", meterStop=""),
    ]
    raw[1].pop("vehicle")
    first, second = build_export(raw, 2026).records
    assert (first.meter_start, first.meter_stop, first.identifier, first.vehicle) == (None, None, "", "")
    assert (second.meter_start, second.meter_stop, second.vehicle) == (None, None, "")


def test_energy_rounding_and_meter_decimals():
    record = build_export([item(1, "2026-03-01T10:00:00+01:00")], 2026).records[0]
    assert record.energy_kwh == Decimal("6.332")
    assert str(record.energy_kwh) == "6.332"
    assert record.meter_start == Decimal("1555.273")
    assert build_export([item(1, "2026-03-01T10:00:00+01:00", chargedEnergy=0.0625)], 2026).records[0].energy_kwh == (
        Decimal("0.063")
    )


def test_total_kwh():
    raw = [
        item(1, "2026-03-01T10:00:00+01:00", chargedEnergy=1.5),
        item(2, "2026-03-02T10:00:00+01:00", chargedEnergy=2.25),
    ]
    assert build_export(raw, 2026).total_kwh == Decimal("3.750")
    assert build_export([], 2026).total_kwh == Decimal("0.000")


def test_records_oldest_first_across_offsets():
    raw = [
        item(1, "2026-07-01T10:00:00+02:00"),
        item(2, "2026-03-01T10:00:00+01:00"),
        item(3, "2026-07-01T09:30:00+01:00"),
    ]
    assert [r.id for r in build_export(raw, 2026).records] == [2, 1, 3]


def test_csv_header_and_row(tmp_path):
    raw = [item(94, "2026-11-19T07:46:34.939292212+01:00", "2026-11-19T08:24:04.940716324+01:00", meterStop=None)]
    path = write_csv(build_export(raw, 2026), tmp_path / "cdr.csv")
    assert path == tmp_path / "cdr.csv"
    assert path.read_bytes().startswith(b"session_id,charge_point,start,end,energy_kwh,meter_start_kwh,")
    assert b"\r\n" in path.read_bytes()
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == HEADER == [
        "session_id", "charge_point", "start", "end", "energy_kwh", "meter_start_kwh", "meter_stop_kwh", "identifier",
        "vehicle",
    ]
    assert rows[1] == ["94", "Carport", "2026-11-19T07:46:34+01:00", "2026-11-19T08:24:04+01:00", "6.332", "1555.273",
                       "", "nfc-0123ABCD", "Carla"]
    assert len(rows) == 2
    assert list(tmp_path.iterdir()) == [path]


def test_csv_guards_formula_injection(tmp_path):
    raw = [
        item(1, "2026-03-01T10:00:00+01:00", loadpoint="=SUM(A1)", identifier="@cmd", vehicle="-1"),
        item(2, "2026-03-02T10:00:00+01:00", loadpoint="+1", identifier="\tx", vehicle="\rx"),
        item(3, "2026-03-03T10:00:00+01:00", loadpoint="Carport", identifier="a=b", vehicle="Carla"),
    ]
    path = write_csv(build_export(raw, 2026), tmp_path / "cdr.csv")
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))[1:]
    assert [rows[0][1], rows[0][7], rows[0][8]] == ["'=SUM(A1)", "'@cmd", "'-1"]
    assert [rows[1][1], rows[1][7], rows[1][8]] == ["'+1", "'\tx", "'\rx"]
    assert [rows[2][1], rows[2][7], rows[2][8]] == ["Carport", "a=b", "Carla"]


def test_write_overwrites_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "cdr.csv"
    target.write_text("old")
    write_csv(build_export([item(1, "2026-03-01T10:00:00+01:00")], 2026), target)
    assert target.read_text(encoding="utf-8").startswith("session_id,")
    assert [p.name for p in tmp_path.iterdir()] == ["cdr.csv"]
