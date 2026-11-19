import json
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
    assert due_periods(date(2026, 11, 1), 2, None) == []
    assert due_periods(date(2026, 11, 2), 2, None) == [OCTOBER]
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


def test_markers_move_to_a_new_state_folder(run, monkeypatch, tmp_path):
    out, mails, _ = run
    cli.create_and_send(OCTOBER)
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    cli.create_and_send(OCTOBER)
    assert len(mails) == 1 and (tmp_path / "state" / "EV-2026-10.sent").exists()


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
    statement = Statement("EV-2026-10", date(2026, 11, 2), OCTOBER, Party("A. Person"), Party("Holding B.V."),
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
    statement = Statement("EV-2026-10", date(2026, 11, 2), OCTOBER, Party("A. Person", "Street 1\n1234 AB Town"),
                          Party("Holding B.V."), "NL00TEST0123456789", "Polestar 2",
                          select_sessions(RAW, ["Carla"], OCTOBER))
    assert statement.title == "Declaratie laadkosten"
    text = PdfReader(render_pdf(statement, tmp_path / "s.pdf")).pages[0].extract_text()
    assert "€ 4,34" in text and "EV-2026-10" in text and "oktober 2026" in text
