import fcntl
import json
import re
import threading
from datetime import date, datetime
from zoneinfo import ZoneInfo
from decimal import Decimal

import pytest
from pypdf import PdfReader

from evcc_chargeback import __main__ as cli
from evcc_chargeback.__main__ import due_periods, next_run, previous_month
from evcc_chargeback.pricing import parse_tariffs
from evcc_chargeback import sessions as api
from evcc_chargeback.mailer import NotSent, PartialDelivery
from evcc_chargeback.sessions import doubtful_sessions, parse_time, select_sessions, total_cost
from evcc_chargeback.statement import Party, Statement, nl_number, render_pdf

OCTOBER = date(2026, 10, 1)
AMSTERDAM = ZoneInfo("Europe/Amsterdam")


def session(id, created, finished, vehicle="Carla", energy=10.0, price=3.333):
    return {"id": id, "created": created, "finished": finished, "vehicle": vehicle, "loadpoint": "Carport",
            "chargedEnergy": energy, "price": price, "solarPercentage": 25.0, "meterStart": 100.0,
            "meterStop": 100.0 + energy, "identifier": "nfc-01"}


RAW = [
    session(1, "2026-10-31T23:30:00.123456789+01:00", "2026-11-01T06:00:00.5+01:00"),  # starts in October
    session(2, "2026-10-02T18:00:00+02:00", "2026-10-02T22:00:00+02:00", price=1.005),
    session(3, "2026-10-03T18:00:00+02:00", "2026-10-03T22:00:00+02:00", vehicle="Guest car"),
    session(4, "2026-09-30T23:00:00+02:00", "2026-10-01T05:00:00+02:00"),  # starts in September
]


def test_selects_vehicle_and_start_month_oldest_first():
    assert [s.id for s in select_sessions(RAW, ["Carla"], OCTOBER)] == [2, 1]


def test_total_is_sum_of_rounded_lines():
    sessions = select_sessions(RAW, ["Carla"], OCTOBER)
    assert [s.cost for s in sessions] == [Decimal("1.01"), Decimal("3.33")]
    assert total_cost(sessions) == Decimal("4.34")


def test_running_session_is_an_error():
    raw = [session(5, "2026-10-31T22:00:00+01:00", "0001-01-01T00:00:00Z")]
    with pytest.raises(ValueError, match="still running"):
        select_sessions(raw, ["Carla"], OCTOBER)


def test_nanosecond_timestamps_parse():
    assert parse_time("2026-10-28T19:03:38.109880592+01:00").microsecond == 109880


def test_previous_month_wraps_year():
    assert previous_month(date(2027, 1, 2)) == date(2026, 12, 1)
    assert previous_month(date(2026, 11, 2)) == OCTOBER


def test_next_run_is_today_or_tomorrow():
    assert next_run(datetime(2026, 11, 2, 6, 59), "07:00") == datetime(2026, 11, 2, 7, 0)
    assert next_run(datetime(2026, 11, 2, 7, 0), "07:00") == datetime(2026, 11, 3, 7, 0)
    assert next_run(datetime(2026, 11, 30, 8, 0), "07:00") == datetime(2026, 12, 1, 7, 0)


def test_dutch_number_format():
    assert nl_number(Decimal("1234.5"), 2) == "1.234,50"


def test_agreed_tariff_of_the_start_day_sets_the_amount():
    tariffs = parse_tariffs("2026-01-01:0.30,2026-10-03:0.25")
    raw = [session(1, "2026-10-02T23:50:00+02:00", "2026-10-03T06:00:00+02:00", energy=10.5, price=None),
           session(2, "2026-10-03T00:10:00+02:00", "2026-10-03T06:00:00+02:00", energy=10.5)]
    sessions = select_sessions(raw, ["Carla"], OCTOBER, tariffs)
    assert [(s.price_per_kwh, s.cost) for s in sessions] == [(Decimal("0.30"), Decimal("3.15")),
                                                               (Decimal("0.25"), Decimal("2.63"))]
    assert sessions[0].evcc_cost is None and sessions[1].evcc_cost == Decimal("3.33")


def test_evcc_basis_needs_a_price():
    raw = [session(1, "2026-10-02T18:00:00+02:00", "2026-10-02T22:00:00+02:00", price=None)]
    with pytest.raises(ValueError, match="no price"):
        select_sessions(raw, ["Carla"], OCTOBER)


def test_renamed_vehicle_matches_both_titles():
    raw = [session(1, "2026-10-02T18:00:00+02:00", "2026-10-02T22:00:00+02:00", vehicle="Polestar 2"), *RAW]
    assert [s.id for s in select_sessions(raw, ["Carla", "Polestar 2"], OCTOBER)] == [1, 2, 1]


def test_doubtful_sessions_of_the_month():
    raw = [*RAW, session(7, "2026-10-05T10:00:00+02:00", "2026-10-05T11:00:00+02:00", vehicle=""),
           session(8, "2026-09-05T10:00:00+02:00", "2026-09-05T11:00:00+02:00", vehicle=None)]
    assert doubtful_sessions(raw, ["Carla"], [], OCTOBER, AMSTERDAM) == [7]
    # Session 3 was started with the tag of the company car, but evcc has another vehicle for it.
    assert doubtful_sessions(raw, ["Carla"], ["NFC-01"], OCTOBER, AMSTERDAM) == [3, 7]


def test_month_is_decided_in_the_billing_time_zone():
    raw = [session(1, "2026-10-31T23:30:00Z", "2026-11-01T02:00:00Z"),  # 00:30 on 1 November in Amsterdam
           session(2, "2026-09-30T22:30:00Z", "2026-10-01T02:00:00Z")]  # 00:30 on 1 October in Amsterdam
    selected = select_sessions(raw, ["Carla"], OCTOBER)
    assert [s.id for s in selected] == [2] and selected[0].start.hour == 0


def test_car_connected_since_before_month_end_keeps_the_month_open(monkeypatch):
    state = {"loadpoints": [
        {"title": "A", "connected": True, "connectedDuration": 3 * 86400, "vehicleTitle": "Carla"},
        {"title": "B", "connected": True, "connectedDuration": 3600, "vehicleTitle": "Carla"},
        {"title": "C", "connected": False, "connectedDuration": 9 * 86400, "vehicleTitle": "Carla"},
        {"title": "D", "connected": True, "connectedDuration": 3 * 86400, "vehicleTitle": "Guest car"},
        {"title": "E", "connected": True, "connectedDuration": 3 * 86400, "vehicleTitle": ""}]}
    monkeypatch.setattr(api, "_get", lambda url, path: state)
    now = datetime(2026, 11, 2, 7, 0, tzinfo=AMSTERDAM)
    assert api.open_sessions("http://evcc.invalid", OCTOBER, ["Carla"], AMSTERDAM, now) == ["A", "E"]


def test_fetch_period_reads_the_neighbour_months_once(monkeypatch):
    asked = []
    monkeypatch.setattr(api, "fetch_sessions", lambda url, year, month: asked.append((year, month)) or RAW[:2])
    assert len(api.fetch_period("http://evcc.invalid", date(2026, 12, 1))) == 2
    assert asked == [(2026, 11), (2026, 12), (2027, 1)]


def test_due_periods():
    assert due_periods(date(2026, 11, 1), 2, OCTOBER) == []
    assert due_periods(date(2026, 11, 2), 2, OCTOBER) == [OCTOBER]
    assert due_periods(date(2026, 12, 1), 2, OCTOBER) == [OCTOBER]  # October failed all November: still due
    assert due_periods(date(2027, 1, 2), 2, OCTOBER) == [OCTOBER, date(2026, 11, 1), date(2026, 12, 1)]
    assert due_periods(date(2026, 11, 2), 2, date(2026, 11, 1)) == []


def test_schedule_does_not_combine_with_one_off_options(run):
    with pytest.raises(SystemExit):
        cli.main(["--schedule", "--dry-run"])


@pytest.fixture
def run(tmp_path, monkeypatch):
    """Configure the tool, serve RAW as the evcc sessions and collect the mails instead of sending them."""
    config = {"EVCC_URL": "http://evcc.invalid", "EVCC_VEHICLE": "Carla", "PRICE_BASIS": "evcc",
              "CLAIMANT_NAME": "A. Person", "CLAIMANT_IBAN": "NL91 abna 0417 1643 00", "PAYER_NAME": "Holding B.V.",
              "SMTP_HOST": "smtp.invalid", "SMTP_USER": "u", "SMTP_PASSWORD": "p", "MAIL_FROM": "a@example.org",
              "MAIL_TO": "b@example.org", "OUTPUT_DIR": str(tmp_path)}
    for name, value in config.items():
        monkeypatch.setenv(name, value)
    mails, state = [], {"raw": RAW, "fail": None}
    monkeypatch.setattr(cli, "fetch_period", lambda *args, **kwargs: state["raw"])
    monkeypatch.setattr(cli, "open_sessions", lambda *args, **kwargs: state.get("open", []))

    def send(subject, text, **kwargs):
        if state["fail"]:
            raise state["fail"]
        mails.append((subject, kwargs["recipients"], kwargs["attachment"]))

    monkeypatch.setattr(cli.mailer, "send", send)
    return tmp_path, mails, state


def test_sends_once_and_keeps_a_record(run):
    out, mails, _ = run
    cli.create_and_send(OCTOBER)
    cli.create_and_send(OCTOBER)
    assert len(mails) == 1 and mails[0][2] == out / "EV-2026-10.pdf"
    record = json.loads((out / "EV-2026-10.json").read_text())
    assert record["total"] == "4.34" and [i["id"] for i in record["evcc_sessions"]] == [1, 2]
    assert (out / "EV-2026-10.sent").exists() and not (out / "EV-2026-10.sending").exists()


def test_dry_run_writes_a_draft_and_leaves_the_sent_statement(run):
    out, mails, state = run
    cli.create_and_send(OCTOBER)
    sent_pdf = (out / "EV-2026-10.pdf").read_bytes()
    state["raw"] = RAW[1:2]
    cli.create_and_send(OCTOBER, dry_run=True)
    assert len(mails) == 1 and (out / "EV-2026-10-draft.pdf").exists()
    assert (out / "EV-2026-10.pdf").read_bytes() == sent_pdf


def test_refused_mail_is_tried_again(run):
    out, mails, state = run
    state["fail"] = NotSent("connection refused")
    with pytest.raises(NotSent):
        cli.create_and_send(OCTOBER)
    assert not (out / "EV-2026-10.sent").exists() and not (out / "EV-2026-10.sending").exists()
    state["fail"] = None
    cli.create_and_send(OCTOBER)
    assert len(mails) == 1


def test_partly_refused_mail_is_not_sent_again(run):
    out, mails, state = run
    state["fail"] = PartialDelivery("refused b@example.org")
    with pytest.raises(PartialDelivery):
        cli.create_and_send(OCTOBER)
    state["fail"] = None
    with pytest.raises(RuntimeError, match="stopped halfway"):
        cli.create_and_send(OCTOBER)
    assert mails == []


def test_unknown_send_result_is_not_sent_again(run):
    out, mails, state = run
    state["fail"] = TimeoutError("no answer after DATA")
    with pytest.raises(TimeoutError):
        cli.create_and_send(OCTOBER)
    assert (out / "EV-2026-10.sending").exists()
    state["fail"] = None
    with pytest.raises(RuntimeError, match="stopped halfway"):
        cli.create_and_send(OCTOBER)
    assert mails == [] and not (out / "EV-2026-10.sent").exists()


def test_force_over_an_interrupted_send_is_a_correction(run):
    out, mails, state = run
    state["fail"] = TimeoutError("no answer after DATA")
    with pytest.raises(TimeoutError):
        cli.create_and_send(OCTOBER)
    first_pdf = (out / "EV-2026-10.pdf").read_bytes()
    state["fail"], state["raw"] = None, RAW[1:2]
    cli.create_and_send(OCTOBER, force=True)
    replaced = {path.suffix: path for path in out.glob("EV-2026-10.replaced-*")}
    assert sorted(replaced) == [".json", ".pdf", ".sending"] and replaced[".pdf"].read_bytes() == first_pdf
    text = PdfReader(out / "EV-2026-10.pdf").pages[0].extract_text()
    assert len(mails) == 1 and "gecorrigeerd" in mails[0][0]
    assert f"op {date.today().isoformat()} is verzonden" in text  # the date only, not the time of the marker
    assert (out / "EV-2026-10.sent").exists() and not (out / "EV-2026-10.sending").exists()


def test_dry_run_changes_no_marker(run):
    out, mails, state = run
    state["raw"] = []
    cli.create_and_send(OCTOBER)
    markers = {path.name: path.read_text() for path in out.iterdir() if path.name.startswith("EV-")}
    state["raw"] = RAW
    cli.create_and_send(OCTOBER, dry_run=True)
    after = {path.name: path.read_text() for path in out.iterdir()
             if path.name.startswith("EV-") and path.name != "EV-2026-10-draft.pdf"}
    assert after == markers == {"EV-2026-10.empty": markers["EV-2026-10.empty"]} and mails == []


def test_forced_correction_to_zero_is_refused(run):
    out, mails, state = run
    cli.create_and_send(OCTOBER)
    state["raw"] = []
    with pytest.raises(RuntimeError, match="by hand"):
        cli.create_and_send(OCTOBER, force=True)
    assert not (out / "EV-2026-10.empty").exists() and (out / "EV-2026-10.sent").exists()
    assert len(mails) == 1 and list(out.glob("*.replaced-*")) == []


def test_failed_correction_reopens_the_month(run):
    out, mails, state = run
    cli.create_and_send(OCTOBER)
    state["raw"], state["fail"] = RAW[1:2], NotSent("refused")
    with pytest.raises(NotSent):
        cli.create_and_send(OCTOBER, force=True)
    state["fail"] = None
    cli.create_and_send(OCTOBER)  # the scheduled run sends the correction
    assert len(mails) == 2 and "gecorrigeerd" in mails[1][0]


def test_month_without_sessions_is_done_and_current_month_is_refused(run):
    out, mails, state = run
    state["raw"] = []
    cli.create_and_send(OCTOBER)
    assert (out / "EV-2026-10.empty").exists() and mails == []
    with pytest.raises(ValueError, match="has not ended"):
        cli.create_and_send(date.today().replace(day=1))


def test_open_session_stops_the_statement_but_not_the_draft(run):
    out, mails, state = run
    state["open"] = ["Carport"]
    with pytest.raises(ValueError, match="Carport"):
        cli.create_and_send(OCTOBER)
    cli.create_and_send(OCTOBER, dry_run=True)
    assert mails == [] and (out / "EV-2026-10-draft.pdf").exists()


def test_interrupted_send_is_not_repeated_without_force(run):
    out, mails, _ = run
    (out / "EV-2026-10.sending").write_text("2026-11-02T07:00:00\n")
    with pytest.raises(RuntimeError, match="stopped halfway"):
        cli.create_and_send(OCTOBER)
    cli.create_and_send(OCTOBER, force=True)
    assert len(mails) == 1 and not (out / "EV-2026-10.sending").exists()


def test_force_keeps_the_replaced_statement(run):
    out, mails, state = run
    cli.create_and_send(OCTOBER)
    state["raw"] = RAW[1:2]
    cli.create_and_send(OCTOBER, force=True)
    assert len(mails) == 2 and len(list(out.glob("EV-2026-10.replaced-*"))) == 3
    assert json.loads((out / "EV-2026-10.json").read_text())["total"] == "1.01"
    assert "gecorrigeerd" in mails[1][0] and "Vervangt" in PdfReader(out / "EV-2026-10.pdf").pages[0].extract_text()


def test_unassigned_session_stops_the_statement(run, monkeypatch):
    _, mails, state = run
    state["raw"] = [*RAW, session(7, "2026-10-05T10:00:00+02:00", "2026-10-05T11:00:00+02:00", vehicle="")]
    with pytest.raises(ValueError, match="no vehicle"):
        cli.create_and_send(OCTOBER)
    monkeypatch.setenv("ALLOW_INCOMPLETE", "true")
    cli.create_and_send(OCTOBER)
    assert len(mails) == 1


def test_agreed_basis_needs_valid_tariffs(run, monkeypatch):
    monkeypatch.setenv("PRICE_BASIS", "agreed")
    monkeypatch.setenv("AGREEMENT_REF", "overeenkomst van 01-10-2026")
    monkeypatch.setenv("AGREED_TARIFFS", "2026-10-01:0,30")
    with pytest.raises(SystemExit, match="AGREED_TARIFFS"):
        cli.check_config(mail=True)
    monkeypatch.setenv("AGREED_TARIFFS", "2026-10-01:0.30")
    cli.create_and_send(OCTOBER)
    assert json.loads((run[0] / "EV-2026-10.json").read_text())["total"] == "6.00"


def test_evcc_basis_with_an_agreement_is_a_settlement(run, monkeypatch):
    monkeypatch.setenv("AGREEMENT_REF", "de overeenkomst van 01-01-2027")
    cli.create_and_send(OCTOBER)
    text = PdfReader(run[0] / "EV-2026-10.pdf").pages[0].extract_text()
    assert run[1][0][0].startswith("Afrekening laadstroom") and "01-01-2027" in text
    assert "€ 4,34" in text and "teruglevertarief" in text


def test_iban_is_checked_and_normalised(run, monkeypatch):
    assert cli.iban() == "NL91ABNA0417164300"
    monkeypatch.setenv("CLAIMANT_IBAN", "NL91ABNA0417164301")
    with pytest.raises(SystemExit, match="control number"):
        cli.iban()


def test_config_check_lists_all_faults(run, monkeypatch):
    monkeypatch.setenv("CLAIMANT_IBAN", "NL00TEST0123456789")
    monkeypatch.setenv("MAIL_TO", "a@example.org\nnot-an-address")
    monkeypatch.setenv("FIRST_PERIOD", "0000-01")
    monkeypatch.setenv("RUN_AT", "25:00")
    monkeypatch.delenv("PAYER_NAME")
    with pytest.raises(SystemExit) as fault:
        cli.check_config(mail=True, scheduled=True)
    cli_fault = str(fault.value)
    monkeypatch.setenv("RUN_AT", "07:00")
    monkeypatch.setenv("FIRST_PERIOD", "")
    monkeypatch.setenv("PAYER_NAME", "Holding B.V.")
    monkeypatch.setenv("CLAIMANT_IBAN", "NL91ABNA0417164300")
    monkeypatch.setenv("MAIL_TO", "a@example.org")
    monkeypatch.setenv("RUN_AT", "25:00")
    cli.check_config(mail=False)  # a one-off draft does not use the schedule settings
    monkeypatch.setenv("RUN_AT", "07:00")
    for name, value, part in (("EVCC_VEHICLE", ",", "EVCC_VEHICLE"), ("MAIL_FROM", ",", "MAIL_FROM"),
                              ("MAIL_FROM", "a@example.org,b@example.org", "one mail address"),
                              ("MAIL_TO", ";", "MAIL_TO"), ("MAIL_TO", "a..b@example.org", "a..b"),
                              ("SMTP_PORT", "70000", "70000"), ("RUN_DAY", "32", "RUN_DAY"),
                              ("EVCC_URL", "evcc.local:7070", "http"),
                              ("CLAIMANT_IBAN", "NL14ABNAA417164300", "form of an IBAN")):
        with monkeypatch.context() as patch:
            patch.setenv(name, value)
            with pytest.raises(SystemExit, match=part):
                cli.check_config(mail=True, scheduled=True)
    cli.check_config(mail=True, scheduled=True)
    for part in ("PAYER_NAME", "CLAIMANT_IBAN", "not-an-address", "0000-01", "25:00"):
        assert part in cli_fault


def test_agreed_basis_reports_tariffs_and_reference(run, monkeypatch):
    monkeypatch.setenv("PRICE_BASIS", "agreed")
    monkeypatch.setenv("AGREED_TARIFFS", "nonsense")
    with pytest.raises(SystemExit) as fault:
        cli.check_config(mail=True)
    assert "AGREED_TARIFFS" in str(fault.value) and "AGREEMENT_REF" in str(fault.value)


def test_bad_sender_leaves_no_marker(run, monkeypatch):
    out, mails, _ = run
    monkeypatch.setenv("MAIL_FROM", ",")
    with pytest.raises(SystemExit):
        cli.create_and_send(OCTOBER)
    assert mails == [] and list(out.glob("*.sending")) == []


def test_markers_in_the_output_folder_count_after_state_dir_is_set(run, monkeypatch, tmp_path):
    out, mails, state = run
    cli.create_and_send(OCTOBER)
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    cli.create_and_send(OCTOBER)
    assert len(mails) == 1 and (out / "EV-2026-10.sent").exists()
    assert not (tmp_path / "state" / "EV-2026-10.sent").exists()
    state["raw"] = RAW[1:2]
    cli.create_and_send(OCTOBER, force=True)  # the old marker is archived where it is, the new one is in STATE_DIR
    assert len(mails) == 2 and "gecorrigeerd" in mails[1][0] and len(list(out.glob("EV-2026-10.replaced-*.sent"))) == 1
    assert (tmp_path / "state" / "EV-2026-10.sent").exists() and not (out / "EV-2026-10.sent").exists()


def test_state_dir_as_a_link_to_the_output_folder(run, monkeypatch, tmp_path):
    out, mails, _ = run
    cli.create_and_send(OCTOBER)
    link = tmp_path.parent / f"{tmp_path.name}-link"
    link.symlink_to(out)
    monkeypatch.setenv("STATE_DIR", str(link))
    cli.create_and_send(OCTOBER)
    assert len(mails) == 1 and (out / "EV-2026-10.sent").exists()


def test_draft_does_not_move_markers(run, monkeypatch, tmp_path):
    out, mails, _ = run
    cli.create_and_send(OCTOBER)
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "elsewhere"))
    cli.create_and_send(OCTOBER, dry_run=True)
    assert (out / "EV-2026-10.sent").exists()
    monkeypatch.delenv("STATE_DIR")
    cli.create_and_send(OCTOBER)
    assert len(mails) == 1


def test_unreadable_password_file_leaves_no_marker(run, monkeypatch, tmp_path):
    out, mails, _ = run
    monkeypatch.setenv("SMTP_PASSWORD_FILE", str(tmp_path / "missing"))
    with pytest.raises(OSError):
        cli.create_and_send(OCTOBER)
    assert mails == [] and list(out.glob("*.sending")) == []


def test_valid_settings_that_look_unusual_pass(run, monkeypatch, tmp_path):
    monkeypatch.setenv("EVCC_URL", "HTTP://evcc.local:7070")
    monkeypatch.setenv("MAIL_TO", '"accounts..payable:eu"@example.org')
    monkeypatch.setenv("ALERT_TO", "not an address")  # only the schedule uses it
    basis = tmp_path / "basis"
    basis.write_text("evcc\n")
    monkeypatch.setenv("PRICE_BASIS", "agreed")
    monkeypatch.setenv("PRICE_BASIS_FILE", str(basis))
    cli.check_config(mail=True)
    monkeypatch.setenv("EVCC_URL", "http://:7070")
    with pytest.raises(SystemExit, match="EVCC_URL"):
        cli.check_config(mail=True)


def test_sleep_uses_elapsed_time_across_the_clock_change():
    now = datetime(2027, 3, 28, 1, 50, tzinfo=AMSTERDAM)
    run_at = next_run(now, "03:15")
    assert run_at.timestamp() - now.timestamp() == 25 * 60


def test_addresses_split_on_commas_semicolons_and_lines(run, monkeypatch):
    monkeypatch.setenv("MAIL_TO", "a@example.org; b@example.org\nc@example.org,")
    assert cli.addresses("MAIL_TO") == ["a@example.org", "b@example.org", "c@example.org"]


def test_bad_address_leaves_no_marker(run, monkeypatch):
    out, mails, _ = run
    monkeypatch.setenv("MAIL_TO", "broken address")
    with pytest.raises(SystemExit):
        cli.create_and_send(OCTOBER)
    assert mails == [] and not (out / "EV-2026-10.sending").exists()


def test_markers_go_to_the_state_folder(run, monkeypatch, tmp_path):
    out, mails, _ = run
    state = tmp_path / "state"
    monkeypatch.setenv("STATE_DIR", str(state))
    cli.create_and_send(OCTOBER)
    assert (state / "EV-2026-10.sent").exists() and not (out / "EV-2026-10.sent").exists()
    (out / "EV-2026-10.pdf").unlink()  # the copies can go; the marker still stops a second send
    cli.create_and_send(OCTOBER)
    assert len(mails) == 1


def test_long_agreement_reference_stays_on_the_page(tmp_path):
    statement = Statement("EV-2026-10", datetime(2026, 11, 2, 9, 30, tzinfo=AMSTERDAM), OCTOBER, Party("A. Person"), Party("Holding B.V."),
                          "NL91ABNA0417164300", "Polestar 2", select_sessions(RAW, ["Carla"], OCTOBER),
                          agreement="de laadstroomovereenkomst tussen partijen van 1 januari 2027 <versie 2> & bijlage")
    page = PdfReader(render_pdf(statement, tmp_path / "s.pdf")).pages[0]
    assert "bijlage" in page.extract_text() and "<versie 2>" in page.extract_text()


def test_options_file_fills_the_configuration(tmp_path, monkeypatch):
    options = tmp_path / "options.json"
    options.write_text(json.dumps({"smtp_port": 465, "allow_incomplete": True, "mail_cc": "", "evcc_url": "http://a"}))
    monkeypatch.setenv("EVCC_CHARGEBACK_OPTIONS", str(options))
    monkeypatch.setenv("EVCC_URL", "http://from-env")
    for name in ("SMTP_PORT", "ALLOW_INCOMPLETE", "MAIL_CC"):
        monkeypatch.delenv(name, raising=False)
    cli.load_options()
    assert (cli.env("SMTP_PORT"), cli.env("ALLOW_INCOMPLETE"), cli.env("EVCC_URL")) == ("465", "true", "http://from-env")
    assert cli.env("MAIL_CC", "none") == "none"
    for name in ("SMTP_PORT", "ALLOW_INCOMPLETE"):
        monkeypatch.delenv(name)


def test_pdf_contains_total_and_reference(tmp_path):
    statement = Statement("EV-2026-10", datetime(2026, 11, 2, 9, 30, tzinfo=AMSTERDAM), OCTOBER, Party("A. Person", "Street 1\n1234 AB Town"),
                          Party("Holding B.V."), "NL00TEST0123456789", "Polestar 2",
                          select_sessions(RAW, ["Carla"], OCTOBER))
    assert statement.title == "Declaratie laadkosten"
    text = PdfReader(render_pdf(statement, tmp_path / "s.pdf")).pages[0].extract_text()
    assert "€ 4,34" in text and "EV-2026-10" in text and "oktober 2026" in text


def test_statement_shows_the_date_and_time_it_was_made(tmp_path):
    def datum(minute):
        statement = Statement("EV-2026-10", datetime(2026, 11, 19, 13, minute, 42, tzinfo=AMSTERDAM), OCTOBER,
                              Party("A. Person"), Party("Holding B.V."), "NL00TEST0123456789", "Polestar 2",
                              select_sessions(RAW, ["Carla"], OCTOBER))
        text = PdfReader(render_pdf(statement, tmp_path / f"{minute}.pdf")).pages[0].extract_text()
        return re.search(r"Datum\s*(\d\d-\d\d-\d{4}(?: \d\d:\d\d)?)", text).group(1)

    assert (datum(5), datum(6)) == ("19-11-2026 13:05", "19-11-2026 13:06")  # one day, two different minutes


def test_record_holds_the_moment_with_its_offset(run):
    out, mails, _ = run
    before = datetime.now(AMSTERDAM).replace(microsecond=0)
    cli.create_and_send(OCTOBER)
    issued = json.loads((out / "EV-2026-10.json").read_text())["issued"]
    moment = datetime.fromisoformat(issued)
    assert moment.utcoffset() == AMSTERDAM.utcoffset(moment.replace(tzinfo=None)) and moment >= before
    assert len(issued) == len("2026-11-19T13:05:42+01:00")  # seconds precision, no fraction
    assert f"{moment:%d-%m-%Y %H:%M}" in PdfReader(out / "EV-2026-10.pdf").pages[0].extract_text()


def test_quoted_address_with_an_escaped_quote_passes(run, monkeypatch):
    monkeypatch.setenv("MAIL_TO", r'"payable\"eu"@example.org, c@example.org')
    assert cli.addresses("MAIL_TO") == [r'"payable\"eu"@example.org', "c@example.org"]
    for wrong in ("a..b@example.org", "a b@example.org", "example.org", r'"open\"@example.org'):
        monkeypatch.setenv("MAIL_TO", wrong)
        with pytest.raises(SystemExit, match="not a mail address"):
            cli.addresses("MAIL_TO")


def test_markup_characters_in_names_print_as_text(tmp_path):
    statement = Statement("EV-2026-10", datetime(2026, 11, 2, 9, 30, tzinfo=AMSTERDAM), OCTOBER, Party("A. <Person> & Co", "Street 1 <rear>"),
                          Party("AT&T Holding B.V.", "Postbus 1 & 2"), "NL91ABNA0417164300", "Polestar 2",
                          select_sessions(RAW, ["Carla"], OCTOBER))
    text = PdfReader(render_pdf(statement, tmp_path / "s.pdf")).pages[0].extract_text()
    for part in ("AT&T Holding B.V.", "Postbus 1 & 2", "Street 1 <rear>", "t.n.v. A. <Person> & Co"):
        assert part in text


def test_start_period_is_stored_on_the_first_start(run, monkeypatch, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    assert cli.start_period(state, tmp_path, date(2026, 11, 2)) == OCTOBER
    assert (state / "first-period").read_text() == "2026-10\n"
    assert cli.start_period(state, tmp_path, date(2027, 3, 5)) == OCTOBER  # a later start reads the stored month
    monkeypatch.setenv("FIRST_PERIOD", "2026-08")
    assert cli.start_period(state, tmp_path, date(2027, 3, 5)) == date(2026, 8, 1)
    assert (state / "first-period").read_text() == "2026-10\n"


def test_month_that_failed_all_next_month_stays_due_after_a_restart(run, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    cli.start_period(state, tmp_path, date(2026, 11, 2))  # first start; October then fails every day of November
    first = cli.start_period(state, tmp_path, date(2026, 12, 3))  # restart in December
    assert due_periods(date(2026, 12, 3), 2, first) == [OCTOBER, date(2026, 11, 1)]


def test_damaged_start_period_file_stops_the_schedule(run, tmp_path):
    (tmp_path / "first-period").write_text("oktober\n")
    with pytest.raises(SystemExit, match="first-period"):
        cli.start_period(tmp_path, tmp_path, date(2026, 11, 2))


def test_start_month_from_the_output_folder_after_state_dir_is_set(run, tmp_path):
    (tmp_path / "first-period").write_text("2026-08\n")  # written before STATE_DIR was set
    state = tmp_path / "state"
    state.mkdir()
    assert cli.start_period(state, tmp_path, date(2027, 3, 5)) == date(2026, 8, 1)
    assert not (state / "first-period").exists()


def test_quoted_address_with_a_comma_or_semicolon_is_one_address(run, monkeypatch):
    monkeypatch.setenv("MAIL_TO", r'"payable\,eu"@example.org; "a;b"@example.org, c@example.org')
    assert cli.addresses("MAIL_TO") == [r'"payable\,eu"@example.org', '"a;b"@example.org', "c@example.org"]


def copy_markers_to(state, out):
    """Make the state the one of an earlier version, which wrote the markers to both folders."""
    state.mkdir(exist_ok=True)
    for marker in out.glob("EV-*.s*"):
        if marker.suffix in (".sent", ".sending") and ".replaced-" not in marker.name:
            (state / marker.name).write_text(marker.read_text())


def test_failed_correction_reopens_the_month_with_markers_in_both_folders(run, monkeypatch, tmp_path):
    out, mails, state = run
    cli.create_and_send(OCTOBER)
    copy_markers_to(tmp_path / "state", out)
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    state["raw"], state["fail"] = RAW[1:2], NotSent("refused")
    with pytest.raises(NotSent):
        cli.create_and_send(OCTOBER, force=True)
    assert not (out / "EV-2026-10.sent").exists() and not (tmp_path / "state" / "EV-2026-10.sent").exists()
    state["fail"] = None
    cli.create_and_send(OCTOBER)  # the scheduled run sends the correction, the stale copy does not stop it
    assert len(mails) == 2 and "gecorrigeerd" in mails[1][0]


def test_stale_empty_copy_does_not_hide_a_stopped_send(run, monkeypatch, tmp_path):
    out, mails, state = run
    (out / "EV-2026-10.empty").write_text("2026-11-02\n")  # a stale copy from an earlier version
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "EV-2026-10.sending").write_text("2026-11-03T07:00:00\n")
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    with pytest.raises(RuntimeError, match="stopped halfway"):
        cli.create_and_send(OCTOBER)
    assert mails == []


def test_empty_markers_in_both_folders_go_when_sessions_appear(run, monkeypatch, tmp_path):
    out, mails, state = run
    state["raw"] = []
    cli.create_and_send(OCTOBER)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "EV-2026-10.empty").write_text((out / "EV-2026-10.empty").read_text())
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    state["raw"] = RAW
    cli.create_and_send(OCTOBER, force=True)
    assert len(mails) == 1 and list(tmp_path.glob("**/EV-2026-10.empty")) == []
    cli.create_and_send(OCTOBER)
    assert len(mails) == 1


def test_correction_through_a_linked_state_folder_archives_each_marker_once(run, monkeypatch, tmp_path):
    out, mails, state = run
    cli.create_and_send(OCTOBER)
    link = tmp_path.parent / f"{tmp_path.name}-alias"
    link.symlink_to(out)
    monkeypatch.setenv("STATE_DIR", str(link))
    state["raw"] = RAW[1:2]
    cli.create_and_send(OCTOBER, force=True)
    assert len(mails) == 2 and len(list(out.glob("EV-2026-10.replaced-*.sent"))) == 1
    assert (out / "EV-2026-10.sent").exists() and not (out / "EV-2026-10.sending").exists()


def test_marker_without_a_date_still_makes_a_correction(run):
    out, mails, state = run
    (out / "EV-2026-10.sent").write_text("")
    assert cli.earlier_send("EV-2026-10", out, out) == cli.UNKNOWN_DATE
    (out / "EV-2026-10.sending").write_text("2026-11-02T07:00:00\n")
    assert cli.earlier_send("EV-2026-10", out, out) == "2026-11-02"  # the date of the other marker
    (out / "EV-2026-10.sending").unlink()
    cli.create_and_send(OCTOBER, force=True)
    text = PdfReader(out / "EV-2026-10.pdf").pages[0].extract_text()
    assert len(mails) == 1 and "gecorrigeerd" in mails[0][0] and cli.UNKNOWN_DATE in text


def test_correction_to_zero_over_an_empty_marker_is_refused(run):
    out, mails, state = run
    (out / "EV-2026-10.sending").write_text("")
    state["raw"] = []
    with pytest.raises(RuntimeError, match="by hand"):
        cli.create_and_send(OCTOBER, force=True)
    assert not (out / "EV-2026-10.empty").exists() and (out / "EV-2026-10.sending").exists()


def test_stopped_send_in_the_output_folder_counts_after_state_dir_is_set(run, monkeypatch, tmp_path):
    out, mails, state = run
    state["fail"] = TimeoutError("no answer after DATA")
    with pytest.raises(TimeoutError):
        cli.create_and_send(OCTOBER)
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    state["fail"] = None
    with pytest.raises(RuntimeError, match="--mark-sent"):
        cli.create_and_send(OCTOBER)
    cli.create_and_send(OCTOBER, force=True)
    assert len(mails) == 1 and "gecorrigeerd" in mails[0][0]
    assert len(list(out.glob("EV-2026-10.replaced-*.sending"))) == 1 and not (out / "EV-2026-10.sending").exists()
    assert (tmp_path / "state" / "EV-2026-10.sent").exists()


def test_refused_correction_of_a_stopped_send_is_still_a_correction(run):
    out, mails, state = run
    state["fail"] = TimeoutError("no answer after DATA")
    with pytest.raises(TimeoutError):
        cli.create_and_send(OCTOBER)
    state["fail"] = NotSent("refused")
    with pytest.raises(NotSent):
        cli.create_and_send(OCTOBER, force=True)
    assert [path.suffix for path in out.glob("EV-2026-10.*.s*")] == [".sending"]  # only the archived marker
    assert list(out.glob("EV-2026-10.s*")) == []
    state["fail"] = None
    cli.create_and_send(OCTOBER)  # the scheduled run
    text = PdfReader(out / "EV-2026-10.pdf").pages[0].extract_text()
    assert len(mails) == 1 and "gecorrigeerd" in mails[0][0] and "Vervangt" in text


def stopped_send(out, state):
    state["fail"] = TimeoutError("no answer after DATA")
    with pytest.raises(TimeoutError):
        cli.create_and_send(OCTOBER)
    state["fail"] = None
    (out / "EV-2026-10.sending").write_text("2026-11-02T07:00:00\n")


def test_mark_sent_ends_a_stopped_send_without_a_mail(run):
    out, mails, state = run
    stopped_send(out, state)
    pdf, record = (out / "EV-2026-10.pdf").read_bytes(), (out / "EV-2026-10.json").read_bytes()
    assert cli.main(["--period", "2026-10", "--mark-sent"]) == 0
    assert (out / "EV-2026-10.sent").read_text() == "2026-11-02\n" and not (out / "EV-2026-10.sending").exists()
    assert (out / "EV-2026-10.pdf").read_bytes() == pdf and (out / "EV-2026-10.json").read_bytes() == record
    cli.create_and_send(OCTOBER)  # the scheduled run
    assert mails == []


def test_mark_sent_with_markers_in_both_folders(run, monkeypatch, tmp_path):
    out, mails, state = run
    stopped_send(out, state)
    copy_markers_to(tmp_path / "state", out)
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    cli.mark_sent(OCTOBER)
    assert list(tmp_path.glob("**/EV-2026-10.sending")) == [] and (tmp_path / "state" / "EV-2026-10.sent").exists()


def test_mark_sent_needs_a_stopped_send(run):
    out, mails, state = run
    with pytest.raises(RuntimeError, match="no send that stopped halfway"):
        cli.mark_sent(OCTOBER)
    cli.create_and_send(OCTOBER)
    with pytest.raises(RuntimeError, match="already sent"):
        cli.mark_sent(OCTOBER)
    assert len(mails) == 1 and not (out / "EV-2026-10.sending").exists()


@pytest.mark.parametrize("argv", [["--mark-sent"], ["--period", "2026-10", "--mark-sent", "--force"],
                                  ["--period", "2026-10", "--mark-sent", "--dry-run"], ["--schedule", "--mark-sent"]])
def test_mark_sent_option_combinations_are_refused(run, argv):
    with pytest.raises(SystemExit):
        cli.main(argv)


@pytest.fixture
def alerts(monkeypatch):
    found = []
    monkeypatch.setattr(cli, "alert", lambda subject, text: found.append(f"{subject}\n{text}"))
    return found


def test_scheduled_action_runs_once_until_it_is_set_to_none(run, alerts, tmp_path):
    out, mails, state = run
    cli.create_and_send(OCTOBER)
    cli.run_action(out, out, "resend", OCTOBER)
    cli.run_action(out, out, "resend", OCTOBER)  # a restart with the option still set
    assert len(mails) == 2 and (out / "last-action").read_text() == "resend 2026-10\n"
    cli.run_action(out, out, "none", None)
    assert not (out / "last-action").exists()
    cli.run_action(out, out, "resend", OCTOBER)
    assert len(mails) == 3 and alerts == []


@pytest.mark.parametrize("error", [NotSent("refused"), TimeoutError("no answer after DATA"),
                                   PartialDelivery("refused b@example.org")])
def test_scheduled_action_is_stored_before_it_runs(run, alerts, error):
    out, mails, state = run
    state["fail"] = error
    cli.run_action(out, out, "resend", OCTOBER)
    assert len(alerts) == 1 and "resend 2026-10" in alerts[0] and "change action_id" in alerts[0]
    assert (out / "last-action").read_text() == "resend 2026-10\n"
    state["fail"] = None
    cli.run_action(out, out, "resend", OCTOBER)  # a restart with the option still set
    assert mails == [] and len(alerts) == 1
    cli.run_action(out, out, "none", None)
    cli.run_action(out, out, "resend", OCTOBER)
    assert len(mails) == 1 and len(alerts) == 1


def test_failed_scheduled_action_is_not_tried_again_and_sends_an_alert(run, alerts):
    out, mails, state = run
    cli.run_action(out, out, "mark_sent", OCTOBER)  # nothing stopped halfway yet
    assert len(alerts) == 1 and "mark_sent 2026-10" in alerts[0] and (out / "last-action").exists()
    stopped_send(out, state)
    cli.run_action(out, out, "mark_sent", OCTOBER)  # a restart with the option still set
    assert len(alerts) == 1 and not (out / "EV-2026-10.sent").exists()
    cli.run_action(out, out, "none", None)
    cli.run_action(out, out, "mark_sent", OCTOBER)
    assert len(alerts) == 1 and (out / "EV-2026-10.sent").exists() and mails == []


def test_refused_resend_action_is_sent_by_the_schedule_once(run, alerts):
    out, mails, state = run
    cli.create_and_send(OCTOBER)
    state["raw"], state["fail"] = RAW[1:2], NotSent("refused")
    cli.run_action(out, out, "resend", OCTOBER)  # the start of the schedule
    assert len(alerts) == 1 and len(mails) == 1
    state["fail"] = None
    cli.run_due(date(2026, 11, 19), 2, OCTOBER)  # the first pass sends the correction
    assert len(mails) == 2 and "gecorrigeerd" in mails[1][0]
    cli.run_action(out, out, "resend", OCTOBER)  # a second start with the same options
    cli.run_due(date(2026, 11, 19), 2, OCTOBER)
    assert len(mails) == 2 and len(alerts) == 1


def test_stored_action_in_the_output_folder_counts_after_state_dir_is_set(run, alerts, monkeypatch, tmp_path):
    out, mails, state = run
    cli.create_and_send(OCTOBER)
    (out / "last-action").write_text("resend 2026-10\n")  # written before STATE_DIR was set
    folder = tmp_path / "state"
    folder.mkdir()
    monkeypatch.setenv("STATE_DIR", str(folder))
    cli.run_action(folder, out, "resend", OCTOBER)
    assert len(mails) == 1
    (folder / "last-action").write_text("resend 2026-10\n")
    cli.run_action(folder, out, "none", None)
    assert not (out / "last-action").exists() and not (folder / "last-action").exists()


@pytest.mark.parametrize("folder", [False, True])
def test_mark_sent_needs_the_pdf(run, folder):
    out, mails, state = run
    stopped_send(out, state)
    (out / "EV-2026-10.pdf").unlink()
    if folder:
        (out / "EV-2026-10.pdf").mkdir()
    with pytest.raises(RuntimeError, match="no statement to mark: EV-2026-10\\.pdf is not a file.*save its "
                                           "attachment as EV-2026-10\\.pdf.*--force"):
        cli.mark_sent(OCTOBER)
    assert (out / "EV-2026-10.sending").exists() and not (out / "EV-2026-10.sent").exists()


@pytest.mark.parametrize("folder", [False, True])
def test_mark_sent_without_the_record_marks_the_month_and_warns(run, caplog, folder):
    out, mails, state = run
    stopped_send(out, state)
    (out / "EV-2026-10.json").unlink()
    if folder:
        (out / "EV-2026-10.json").mkdir()
    with caplog.at_level("WARNING", logger="evcc_chargeback"):
        cli.mark_sent(OCTOBER)
    assert (out / "EV-2026-10.sent").read_text() == "2026-11-02\n" and not (out / "EV-2026-10.sending").exists()
    assert "EV-2026-10 is marked as sent without its record" in caplog.text
    assert "source data of the statement is not kept" in caplog.text
    assert mails == []


def test_archived_sent_marker_date_comes_before_the_sending_date(run):
    out, mails, state = run
    (out / "EV-2026-10.replaced-20261101T070000000000.sent").write_text("2026-11-01\n")
    (out / "EV-2026-10.replaced-20261105T070000000000.sending").write_text("2026-11-04T07:00:00\n")
    (out / "EV-2026-10.replaced-20261105T070000000000.sent").write_text("2026-11-03\n")
    assert cli.earlier_send("EV-2026-10", out, out) == "2026-11-03"


def test_config_check_reports_a_bad_action(run, monkeypatch):
    cli.check_config(mail=True, scheduled=True)
    this_month = date.today().strftime("%Y-%m")
    for action, period, part in (("again", "2026-10", "ACTION 'again'"), ("resend", "", "ACTION_PERIOD"),
                                 ("mark_sent", "2026-13", "ACTION_PERIOD"),
                                 ("resend", this_month, "has not ended")):
        with monkeypatch.context() as patch:
            patch.setenv("ACTION", action)
            patch.setenv("ACTION_PERIOD", period)
            with pytest.raises(SystemExit, match=part):
                cli.check_config(mail=True, scheduled=True)
    monkeypatch.setenv("ACTION", "none")
    monkeypatch.setenv("ACTION_PERIOD", "nonsense")  # not used without an action
    cli.check_config(mail=True, scheduled=True)


def test_changed_action_id_runs_the_action_again_in_one_start(run, alerts):
    out, mails, state = run
    cli.create_and_send(OCTOBER)
    cli.run_action(out, out, "resend", OCTOBER)
    assert len(mails) == 2 and (out / "last-action").read_text() == "resend 2026-10\n"
    cli.run_action(out, out, "resend", OCTOBER, "2")  # a new id, without a start with ACTION=none
    assert len(mails) == 3 and (out / "last-action").read_text() == "resend 2026-10 2\n"
    cli.run_action(out, out, "resend", OCTOBER, "2")  # a restart with the same id
    assert len(mails) == 3 and alerts == []


def test_stored_request_without_an_id_is_the_same_as_an_empty_id(run, alerts):
    out, mails, state = run
    (out / "last-action").write_text("resend 2026-10\n")  # written by a version without ACTION_ID
    cli.run_action(out, out, "resend", OCTOBER, "")
    assert mails == [] and (out / "last-action").read_text() == "resend 2026-10\n"


def test_action_id_from_the_options(run, monkeypatch):
    monkeypatch.setenv("ACTION", "resend")
    monkeypatch.setenv("ACTION_PERIOD", "2026-10")
    assert cli.action() == ("resend", OCTOBER, "")
    monkeypatch.setenv("ACTION_ID", "retry-2.b_3")
    assert cli.action() == ("resend", OCTOBER, "retry-2.b_3")


@pytest.mark.parametrize("ident", ["two words", "a/b", "x" * 33, "é"])
def test_config_check_reports_a_bad_action_id(run, monkeypatch, ident):
    monkeypatch.setenv("ACTION", "resend")
    monkeypatch.setenv("ACTION_PERIOD", "2026-10")
    monkeypatch.setenv("ACTION_ID", ident)
    with pytest.raises(SystemExit, match="ACTION_ID"):
        cli.check_config(mail=True, scheduled=True)


def test_repeated_resend_with_a_new_id_sends_one_statement_in_one_start(run, alerts):
    """The refused first resend leaves an open correction; the start with a new id must not send it twice."""
    out, mails, state = run
    cli.create_and_send(OCTOBER)
    state["raw"], state["fail"] = RAW[1:2], NotSent("refused")
    cli.run_action(out, out, "resend", OCTOBER)
    assert len(mails) == 1 and cli.open_corrections(out, out) == [OCTOBER]
    state["fail"] = None
    # One start of the schedule: the action first, then the pass.
    cli.run_action(out, out, "resend", OCTOBER, "2")
    cli.run_due(date(2026, 11, 19), 2, OCTOBER)
    assert len(mails) == 2 and "gecorrigeerd" in mails[1][0] and len(alerts) == 1
    assert (out / "EV-2026-10.sent").exists() and cli.open_corrections(out, out) == []


NOVEMBER = date(2026, 11, 1)


def test_refused_resend_before_the_start_month_is_sent_by_the_schedule_once(run, alerts):
    out, mails, state = run
    cli.create_and_send(OCTOBER)
    state["raw"], state["fail"] = RAW[1:2], NotSent("refused")
    cli.run_action(out, out, "resend", OCTOBER)
    assert cli.open_corrections(out, out) == [OCTOBER] and due_periods(date(2026, 11, 19), 2, NOVEMBER) == []
    state["fail"] = None
    cli.run_due(date(2026, 11, 19), 2, NOVEMBER)  # FIRST_PERIOD=2026-11
    assert len(mails) == 2 and "EV-2026-10, gecorrigeerd" in mails[1][0]
    cli.run_due(date(2026, 11, 19), 2, NOVEMBER)
    assert len(mails) == 2 and len(alerts) == 1 and cli.open_corrections(out, out) == []


def test_month_before_the_start_month_without_a_correction_is_not_sent(run, alerts):
    out, mails, state = run
    cli.run_due(date(2026, 11, 19), 2, NOVEMBER)
    stopped_send(out, state)  # a live .sending marker: the user decides, not the schedule
    (out / "EV-2026-10.replaced-20261101T070000000000.sent").write_text("2026-11-01\n")
    cli.run_due(date(2026, 11, 19), 2, NOVEMBER)
    assert mails == [] and alerts == [] and cli.open_corrections(out, out) == []


def lock_is_held(out):
    with (out / ".lock").open("w") as other:
        try:
            cli.fcntl.flock(other, cli.fcntl.LOCK_EX | cli.fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        cli.fcntl.flock(other, cli.fcntl.LOCK_UN)
        return False


def test_scheduled_resend_is_stored_and_locked_before_the_send(run, alerts, monkeypatch):
    out, mails, state = run
    seen = []
    send = cli.mailer.send

    def check(*args, **kwargs):
        seen.append(((out / "last-action").read_text(), lock_is_held(out)))
        send(*args, **kwargs)

    monkeypatch.setattr(cli.mailer, "send", check)
    cli.run_action(out, out, "resend", OCTOBER)
    assert seen == [("resend 2026-10\n", True)] and len(mails) == 1 and alerts == []
    assert not lock_is_held(out)


def test_scheduled_mark_sent_runs_with_the_lock_held(run, alerts, monkeypatch):
    out, mails, state = run
    stopped_send(out, state)
    seen = []
    mark = cli._mark_sent
    monkeypatch.setattr(cli, "_mark_sent", lambda period: (seen.append(lock_is_held(out)), mark(period)))
    cli.run_action(out, out, "mark_sent", OCTOBER)
    assert seen == [True] and (out / "EV-2026-10.sent").exists() and alerts == []


def test_same_action_at_a_next_start_says_it_was_started_before(run, alerts, caplog):
    out, mails, state = run
    (out / "last-action").write_text("resend 2026-10\n")  # the process stopped after the write
    with caplog.at_level("INFO", logger="evcc_chargeback"):
        cli.run_action(out, out, "resend", OCTOBER)
    assert mails == [] and "was done already" not in caplog.text
    assert "resend 2026-10 was started at an earlier start and does not run again" in caplog.text
    assert "check its result in the log or the mailbox" in caplog.text and "change action_id" in caplog.text


def test_action_logs_its_start_time_and_the_statement_shows_it(run, alerts, caplog, monkeypatch):
    out, mails, state = run
    started = datetime(2026, 11, 19, 13, 5, 59, tzinfo=AMSTERDAM)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return started.astimezone(tz) if tz else started.replace(tzinfo=None)

    monkeypatch.setattr(cli, "datetime", Clock)
    with caplog.at_level("INFO", logger="evcc_chargeback"):
        cli.run_action(out, out, "resend", OCTOBER)
    assert "action resend 2026-10 starts at 19-11-2026 13:05" in caplog.text and len(mails) == 1
    assert json.loads((out / "EV-2026-10.json").read_text())["issued"] == "2026-11-19T13:05:59+01:00"
    assert "19-11-2026 13:05" in PdfReader(out / "EV-2026-10.pdf").pages[0].extract_text()
    caplog.clear()
    with caplog.at_level("INFO", logger="evcc_chargeback"):
        cli.run_action(out, out, "resend", OCTOBER)  # a restart: skipped, so no start line
    assert "starts at" not in caplog.text and "was started at an earlier start" in caplog.text


@pytest.mark.parametrize("moment, expected", [
    (datetime(2026, 10, 25, 2, 30, fold=0), True),   # summer time, the first 02:30
    (datetime(2026, 10, 25, 2, 30, fold=1), True),   # winter time, the second 02:30
    (datetime(2026, 10, 25, 1, 59), False),
    (datetime(2026, 10, 25, 3, 0), False),
    (datetime(2026, 11, 19, 2, 30), False),
    (datetime(2026, 3, 29, 2, 30), False),           # does not exist: the clock goes forward
])
def test_clock_goes_back_in_amsterdam(moment, expected):
    assert cli.clock_goes_back(moment.replace(tzinfo=AMSTERDAM)) is expected


def test_clock_never_goes_back_in_utc():
    for moment in (datetime(2026, 10, 25, 2, 30), datetime(2026, 3, 29, 2, 30), datetime(2026, 11, 19, 2, 30)):
        assert not cli.clock_goes_back(moment.replace(tzinfo=ZoneInfo("UTC")))


def test_action_waits_until_the_clock_has_gone_back(run, alerts, caplog, monkeypatch):
    out, mails, state = run
    SEPTEMBER = date(2026, 9, 1)  # a month that has ended on 25 October
    clock = {"now": datetime(2026, 10, 25, 2, 50, fold=1, tzinfo=AMSTERDAM)}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"].astimezone(tz) if tz else clock["now"].replace(tzinfo=None)

    calls = []
    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(cli, "_create_and_send", lambda period, **kwargs: calls.append((period, kwargs["issued"])))
    with caplog.at_level("INFO", logger="evcc_chargeback"):
        cli.run_action(out, out, "resend", SEPTEMBER)
    assert calls == [] and not (out / "last-action").exists() and len(alerts) == 1
    assert "goes back in this hour" in alerts[0] and "not stored" in alerts[0] and "after this hour ends" in alerts[0]
    assert "starts at" not in caplog.text
    clock["now"] = datetime(2026, 10, 25, 3, 5, tzinfo=AMSTERDAM)  # the next start, after the hour
    cli.run_action(out, out, "resend", SEPTEMBER)
    assert calls == [(SEPTEMBER, clock["now"])] and (out / "last-action").read_text() == "resend 2026-09\n"
    assert len(alerts) == 1


def test_force_waits_until_the_clock_has_gone_back(run, monkeypatch):
    out, mails, state = run
    september = date(2026, 9, 1)  # RAW has one session of the car in September
    clock = {"now": datetime(2026, 10, 24, 12, 0, tzinfo=AMSTERDAM)}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"].astimezone(tz) if tz else clock["now"].replace(tzinfo=None)

    monkeypatch.setattr(cli, "datetime", Clock)
    reads = []
    monkeypatch.setattr(cli, "fetch_period", lambda *args, **kwargs: reads.append(args) or state["raw"])
    assert cli.main(["--period", "2026-09"]) == 0
    files = {path.name: path.read_bytes() for path in out.iterdir()}
    assert len(mails) == 1 and len(reads) == 1 and "EV-2026-09.sent" in files

    clock["now"] = datetime(2026, 10, 25, 2, 30, fold=1, tzinfo=AMSTERDAM)  # the second 02:30
    with pytest.raises(SystemExit, match="goes back in this hour.*after this hour ends"):
        cli.main(["--period", "2026-09", "--force"])
    assert len(mails) == 1 and len(reads) == 1
    assert {path.name: path.read_bytes() for path in out.iterdir()} == files

    clock["now"] = datetime(2026, 10, 25, 3, 5, tzinfo=AMSTERDAM)
    assert cli.main(["--period", "2026-09", "--force"]) == 0
    assert len(mails) == 2 and "gecorrigeerd" in mails[1][0]
    assert json.loads((out / "EV-2026-09.json").read_text())["issued"] == "2026-10-25T03:05:00+01:00"


def test_action_takes_its_one_clock_reading_under_the_lock(run, alerts, monkeypatch):
    """A start that reads 01:59 before it waits for the lock and 02:00 after it must not store the request."""
    out, mails, state = run
    SEPTEMBER = date(2026, 9, 1)  # a month that has ended on 25 October
    clock = {"locked": False, "inside": datetime(2026, 10, 25, 2, 0, fold=0, tzinfo=AMSTERDAM)}
    readings = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = clock["inside"] if clock["locked"] else datetime(2026, 10, 25, 1, 59, 59, tzinfo=AMSTERDAM)
            readings.append((clock["locked"], moment))
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    lock = cli.state_lock

    @cli.contextmanager
    def watched_lock(folder):
        with lock(folder):
            clock["locked"] = True
            try:
                yield
            finally:
                clock["locked"] = False

    calls = []
    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(cli, "state_lock", watched_lock)
    monkeypatch.setattr(cli, "_create_and_send", lambda period, **kwargs: calls.append((period, kwargs["issued"])))
    cli.run_action(out, out, "resend", SEPTEMBER, "2")
    assert calls == [] and not (out / "last-action").exists() and len(alerts) == 1
    assert "goes back in this hour" in alerts[0] and readings == [(True, clock["inside"])]

    clock["inside"] = datetime(2026, 10, 25, 3, 5, tzinfo=AMSTERDAM)  # the restart after the hour, same id
    readings.clear()
    cli.run_action(out, out, "resend", SEPTEMBER, "2")
    assert calls == [(SEPTEMBER, clock["inside"])] and (out / "last-action").read_text() == "resend 2026-09 2\n"
    assert readings == [(True, clock["inside"])] and len(alerts) == 1


@pytest.mark.parametrize("name", ["resend", "mark_sent"])
def test_action_in_its_own_month_is_not_stored(run, alerts, monkeypatch, name):
    """The clock under the lock is in the month of the action, for example after a step back over the month end."""
    out, mails, state = run
    clock = {"now": datetime(2026, 10, 31, 23, 59, tzinfo=AMSTERDAM)}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"].astimezone(tz) if tz else clock["now"].replace(tzinfo=None)

    calls = []
    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(cli, "_create_and_send", lambda period, **kwargs: calls.append((period, kwargs["issued"])))
    monkeypatch.setattr(cli, "_mark_sent", lambda period: calls.append((period, None)))
    cli.run_action(out, out, name, OCTOBER, "3")
    assert calls == [] and not (out / "last-action").exists() and len(alerts) == 1
    assert "oktober 2026 has not ended" in alerts[0] and "not stored" in alerts[0]
    assert "when the month has ended" in alerts[0]

    clock["now"] = datetime(2026, 11, 1, 0, 1, tzinfo=AMSTERDAM)  # the restart in the next month, same id
    cli.run_action(out, out, name, OCTOBER, "3")
    assert calls == [(OCTOBER, clock["now"] if name == "resend" else None)] and len(alerts) == 1
    assert (out / "last-action").read_text() == f"{name} 2026-10 3\n"


def test_stale_empty_marker_does_not_hide_an_open_correction(run, alerts):
    out, mails, state = run
    (out / "EV-2026-10.replaced-20261101T070000000000.sent").write_text("2026-11-01\n")
    (out / "EV-2026-10.empty").write_text("2026-11-02\n")
    state["raw"] = []
    cli.run_due(date(2026, 11, 19), 2, OCTOBER)
    assert mails == [] and len(alerts) == 1 and "cannot be corrected to zero" in alerts[0]
    assert (out / "EV-2026-10.empty").read_text() == "2026-11-02\n" and not (out / "EV-2026-10.sent").exists()
    state["raw"] = RAW
    cli.run_due(date(2026, 11, 19), 2, OCTOBER)
    cli.run_due(date(2026, 11, 19), 2, OCTOBER)
    assert len(mails) == 1 and "gecorrigeerd" in mails[0][0] and len(alerts) == 1
    assert not (out / "EV-2026-10.empty").exists() and cli.open_corrections(out, out) == []


def test_two_starts_at_the_same_moment_do_the_action_once(run, alerts, monkeypatch, caplog):
    """The lock that the second start takes in run_action is held while the first has not written last-action."""
    out, mails, state = run
    first_writes, second_tried, go_on = (threading.Event() for _ in range(3))
    probe = {}
    write, flock = cli.write_atomic, cli.fcntl.flock

    def paused_write(path, data):
        if path.name == "last-action" and threading.current_thread().name == "first":
            first_writes.set()
            assert go_on.wait(10)
        write(path, data)

    def watched_flock(fd, operation):
        if threading.current_thread().name == "second" and operation == fcntl.LOCK_EX:
            # A try without waiting on the same descriptor shows that this acquisition must wait for the first start.
            try:
                flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                probe["refused"] = False
            except BlockingIOError:
                probe["refused"] = True
            second_tried.set()
        return flock(fd, operation)

    monkeypatch.setattr(cli, "write_atomic", paused_write)
    monkeypatch.setattr(cli.fcntl, "flock", watched_flock)
    first = threading.Thread(target=cli.run_action, args=(out, out, "resend", OCTOBER), name="first")
    second = threading.Thread(target=cli.run_action, args=(out, out, "resend", OCTOBER), name="second")
    with caplog.at_level("INFO", logger="evcc_chargeback"):
        first.start()
        try:
            assert first_writes.wait(10)
            second.start()
            assert second_tried.wait(10)
            assert probe["refused"] is True
            assert not (out / "last-action").exists()
        finally:
            go_on.set()
            first.join(10)
            if second.ident is not None:
                second.join(10)
    assert not first.is_alive() and not second.is_alive()
    assert len(mails) == 1 and alerts == []
    assert caplog.text.count("resend 2026-10 was started at an earlier start and does not run again") == 1
