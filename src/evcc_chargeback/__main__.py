"""Create the statement for one month and send it.

Configuration comes from environment variables (see .env.example). Secrets can be given as files with the
*_FILE variant of a variable, for example SMTP_PASSWORD_FILE.
"""

import argparse
import fcntl
import hashlib
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import mailer
from .pricing import Tariff, parse_tariffs
from .sessions import doubtful_sessions, fetch_period, open_sessions, select_sessions
from .statement import Party, Statement, euro, period_label, render_pdf, statement_number

log = logging.getLogger("evcc_chargeback")

STATEMENT = ("EVCC_URL", "EVCC_VEHICLE", "PRICE_BASIS", "CLAIMANT_NAME", "CLAIMANT_IBAN", "PAYER_NAME")
MAIL = ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM", "MAIL_TO")


def env(name: str, default: str | None = None) -> str:
    file = os.environ.get(f"{name}_FILE")
    if file:
        return Path(file).read_text().strip()
    value = os.environ.get(name) or default
    if value is None:
        raise SystemExit(f"missing configuration: {name}")
    return value


def names(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def price_basis() -> tuple[list[Tariff] | None, str]:
    """Return the agreed tariffs (None = the value that evcc recorded) and the agreement reference."""
    basis = env("PRICE_BASIS")
    if basis == "evcc":
        return None, env("AGREEMENT_REF", "")
    if basis != "agreed":
        raise SystemExit("PRICE_BASIS must be 'agreed' or 'evcc'")
    try:
        return parse_tariffs(env("AGREED_TARIFFS")), env("AGREEMENT_REF")
    except ValueError as error:
        raise SystemExit(f"AGREED_TARIFFS: {error}") from None


def billing_zone() -> ZoneInfo:
    return ZoneInfo(env("BILLING_TZ", "Europe/Amsterdam"))


def check_config(mail: bool) -> None:
    for name in STATEMENT + (MAIL if mail else ()):
        env(name)
    price_basis()
    billing_zone()
    if first := env("FIRST_PERIOD", ""):
        parse_period(first)


def previous_month(today: date) -> date:
    return date(today.year - 1, 12, 1) if today.month == 1 else date(today.year, today.month - 1, 1)


def parse_period(value: str) -> date:
    year, month = value.split("-")
    return date(int(year), int(month), 1)


def due_periods(today: date, run_day: int, first: date | None) -> list[date]:
    """Return the months to send on `today`, oldest first.

    The previous month is due from day `run_day` on. With `first` (FIRST_PERIOD) every month from `first` on
    is due until it is sent, so a month that failed until the next month began is not lost.
    """
    last = previous_month(today)
    if today.day < run_day:
        last = previous_month(last)
    if first is None:
        return [last] if today.day >= run_day else []
    periods = []
    while last >= first:
        periods.insert(0, last)
        last = previous_month(last)
    return periods


def next_run(now: datetime, run_at: str) -> datetime:
    hour, minute = (int(part) for part in run_at.split(":"))
    run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return run if run > now else run + timedelta(days=1)


def schedule() -> None:
    """Run every day at RUN_AT from day RUN_DAY of the month on.

    The sent marker makes the runs after a successful send do nothing, so a failed run (evcc down, a session
    still running) is retried the next day.
    """
    run_day, run_at = int(env("RUN_DAY", "2")), env("RUN_AT", "07:00")
    check_config(mail=True)
    first = parse_period(env("FIRST_PERIOD")) if env("FIRST_PERIOD", "") else None
    while True:  # the first pass is at the start, so a restart after RUN_AT does not skip a day
        for period in due_periods(datetime.now(billing_zone()).date(), run_day, first):
            try:
                create_and_send(period)
            except (Exception, SystemExit) as error:
                log.exception("%s failed; next try tomorrow", statement_number(period))
                alert(f"evcc-chargeback: {statement_number(period)} is niet verstuurd",
                      f"{type(error).__name__}: {error}\n")
        run = next_run(datetime.now(), run_at)
        log.info("next run at %s", run.isoformat(timespec="minutes"))
        # Sleep in steps, so a clock change or a suspended host does not shift the run.
        while (left := (run - datetime.now()).total_seconds()) > 0:
            time.sleep(min(left, 3600))


def alert(subject: str, text: str) -> None:
    """Tell ALERT_TO that a scheduled run failed. A failed alert must not stop the schedule."""
    recipients = names(env("ALERT_TO", ""))
    if not recipients:
        return
    try:
        send_mail(subject, text, recipients)
    except Exception:
        log.exception("could not send the alert")


def send_mail(subject: str, text: str, recipients: list[str], cc: list[str] | None = None,
              attachment: Path | None = None) -> None:
    mailer.send(subject, text, attachment=attachment, host=env("SMTP_HOST"), port=int(env("SMTP_PORT", "587")),
                user=env("SMTP_USER"), password=env("SMTP_PASSWORD"), sender=env("MAIL_FROM"),
                recipients=recipients, cc=cc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evcc-chargeback", description=__doc__.splitlines()[0])
    parser.add_argument("--period", type=parse_period, help="month as YYYY-MM (default: previous month)")
    parser.add_argument("--dry-run", action="store_true", help="write a draft PDF, do not send it")
    parser.add_argument("--force", action="store_true", help="make and send again when the period was already sent")
    parser.add_argument("--schedule", action="store_true",
                        help="keep running and send the previous month from day RUN_DAY at RUN_AT")
    args = parser.parse_args(argv)
    if args.schedule and (args.period or args.dry_run or args.force):
        parser.error("--schedule does not go together with other options")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.schedule:
        schedule()
    else:
        check_config(mail=not args.dry_run)
        create_and_send(args.period or previous_month(datetime.now(billing_zone()).date()), args.dry_run, args.force)
    return 0


def output_dir() -> Path:
    output = Path(env("OUTPUT_DIR", "output"))
    output.mkdir(parents=True, exist_ok=True)
    return output


def write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def earlier_send(sent: Path) -> str:
    """Return the date of the last send of this statement, also when --force already moved its marker away."""
    markers = [sent] if sent.exists() else sorted(sent.parent.glob(f"{sent.stem}.replaced-*.sent"))
    return markers[-1].read_text().strip() if markers else ""


def create_and_send(period: date, dry_run: bool = False, force: bool = False) -> None:
    """Make the statement of the month and send it. One run at a time for each output directory."""
    with (output_dir() / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _create_and_send(period, dry_run, force)


def _create_and_send(period: date, dry_run: bool, force: bool) -> None:
    number = statement_number(period)
    output = output_dir()
    zone = billing_zone()
    if not dry_run and period >= datetime.now(zone).date().replace(day=1):
        raise ValueError(f"{period_label(period)} has not ended; only --dry-run is possible")
    empty = output / f"{number}.empty"
    pdf, record = output / f"{number}.pdf", output / f"{number}.json"
    sending, sent = output / f"{number}.sending", output / f"{number}.sent"

    if not dry_run and not force:
        if sent.exists():
            log.info("%s was already sent on %s; use --force to send again", number, sent.read_text().strip())
            return
        if empty.exists():
            log.info("%s had no sessions on %s; use --force to look again", number, empty.read_text().strip())
            return
        if sending.exists():
            raise RuntimeError(f"an earlier send of {number} stopped halfway; look in the mailbox, then use "
                               f"--force to send again or rename {sending.name} to {sent.name}")

    tariffs, agreement = price_basis()
    vehicles = names(env("EVCC_VEHICLE"))
    # Read the connection state first: a car that disconnects after this is in the session list that follows.
    waiting = open_sessions(env("EVCC_URL"), period, vehicles, zone, datetime.now(zone))
    raw = fetch_period(env("EVCC_URL"), period)
    problems = []
    if doubtful := doubtful_sessions(raw, vehicles, names(env("EVCC_IDENTIFIERS", "")), period, zone):
        problems.append(f"sessions {doubtful} have no vehicle, or another vehicle than their RFID tag says; "
                        f"correct the vehicle of these sessions in evcc")
    if waiting:
        problems.append(f"a car has been connected at {', '.join(waiting)} since before the end of the month; "
                        f"its session is complete when it disconnects")
    if problems and env("ALLOW_INCOMPLETE", "false").lower() != "true":
        if not dry_run:
            raise ValueError("; ".join(problems) + " (or set ALLOW_INCOMPLETE=true)")
        log.warning("the draft is not final: %s", "; ".join(problems))
    sessions = select_sessions(raw, vehicles, period, tariffs, zone)
    log.info("%s: %d sessions of %s", number, len(sessions), ", ".join(vehicles))
    if not sessions:
        log.info("no sessions in %s, nothing to send", period_label(period))
        if not dry_run:
            empty.write_text(date.today().isoformat() + "\n")
        return
    empty.unlink(missing_ok=True)

    statement = Statement(
        number=number,
        issue_date=date.today(),
        period=period,
        claimant=Party(env("CLAIMANT_NAME"), env("CLAIMANT_ADDRESS", "").replace("\\n", "\n")),
        payer=Party(env("PAYER_NAME"), env("PAYER_ADDRESS", "").replace("\\n", "\n")),
        iban=env("CLAIMANT_IBAN"),
        vehicle_label=env("VEHICLE_LABEL", vehicles[0]),
        sessions=sessions,
        agreement=agreement,
        fixed_tariff=tariffs is not None,
        replaces=earlier_send(sent),
    )
    if dry_run:
        draft = render_pdf(statement, output / f"{number}-draft.pdf")
        log.info("wrote %s, total %s; nothing was sent", draft, euro(statement.total))
        return

    # The PDF and its record are final from here: replace them only on --force, and keep the old ones.
    # The old sent marker goes too: until the correction is sent, the month is open again.
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    if sent.exists():
        for old in (pdf, record, sent):
            if old.exists():
                old.rename(old.with_name(f"{old.stem}.replaced-{stamp}{old.suffix}"))
    render_pdf(statement, pdf)
    ids = {s.id for s in sessions}
    write_atomic(record, json.dumps({
        "number": number,
        "issued": statement.issue_date.isoformat(),
        "basis": "agreed" if tariffs else "evcc",
        "agreement": agreement,
        "total": str(statement.total),
        "energy_kwh": str(statement.energy),
        "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "lines": [{"id": s.id, "energy_kwh": str(s.energy_kwh), "price_per_kwh": str(s.price_per_kwh),
                   "cost": str(s.cost)} for s in sessions],
        "evcc_sessions": [item for item in raw if item.get("id") in ids],
    }, indent=1).encode())
    log.info("wrote %s and %s, total %s", pdf, record.name, euro(statement.total))

    if force:
        sending.unlink(missing_ok=True)
    with sending.open("x") as marker:  # fails when another run sends this statement right now
        marker.write(datetime.now().isoformat(timespec="seconds") + "\n")
    try:
        send_mail(
            f"{statement.title} {period_label(period)} ({number}{', gecorrigeerd' if statement.replaces else ''})",
            f"Bijgaand de {statement.title.lower()} van {period_label(period)} voor {statement.vehicle_label}.\n\n"
            f"Totaal: {euro(statement.total)}, over te maken naar {statement.iban} o.v.v. {number}.\n",
            names(env("MAIL_TO")), names(env("MAIL_CC", "")), pdf,
        )
    except mailer.NotSent:
        # The server did not accept the message, so the next run can try again.
        sending.unlink()
        raise
    # Every other error keeps the marker: the message may have arrived, so there is no automatic second send.
    sent.write_text(date.today().isoformat() + "\n")
    sending.unlink()
    log.info("sent %s", number)


if __name__ == "__main__":
    sys.exit(main())
