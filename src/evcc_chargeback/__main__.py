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
import re
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from . import mailer
from .pricing import Tariff, parse_tariffs
from .sessions import doubtful_sessions, fetch_period, open_sessions, select_sessions
from .statement import Party, Statement, euro, issued_label, period_label, render_pdf, statement_number

log = logging.getLogger("evcc_chargeback")

STATEMENT = ("EVCC_URL", "EVCC_VEHICLE", "PRICE_BASIS", "CLAIMANT_NAME", "CLAIMANT_IBAN", "PAYER_NAME")
MAIL = ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM", "MAIL_TO")
ACTIONS = ("none", "resend", "mark_sent")
# The statement shows this when an earlier version was sent but its marker holds no date.
UNKNOWN_DATE = "een onbekende datum"
# The sent or sending marker of a statement version that a correction replaced.
ARCHIVED = re.compile(r"EV-(20[0-9]{2}-(?:0[1-9]|1[0-2]))\.replaced-[^.]+\.(?:sent|sending)")


def env(name: str, default: str | None = None) -> str:
    """Return a setting, from the file in NAME_FILE or from NAME. An empty value counts as not set."""
    file = os.environ.get(f"{name}_FILE")
    value = Path(file).read_text().strip("\r\n") if file else os.environ.get(name) or ""
    if name != "SMTP_PASSWORD":  # spaces can be part of a password
        value = value.strip()
    value = value or default
    if value is None:
        raise SystemExit(f"missing configuration: {name}")
    return value


def load_options() -> None:
    """Take values from the JSON file in EVCC_CHARGEBACK_OPTIONS, as written by the Home Assistant Supervisor.

    The key `smtp_host` sets SMTP_HOST. A variable that is already in the environment keeps its value, and an
    empty option counts as not set.
    """
    path = os.environ.get("EVCC_CHARGEBACK_OPTIONS")
    if not path:
        return
    for key, value in json.loads(Path(path).read_text()).items():
        if value is None or value == "":
            continue
        text = str(value).lower() if isinstance(value, bool) else str(value)
        os.environ.setdefault(key.upper(), text)


def names(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def vehicles() -> list[str]:
    found = names(env("EVCC_VEHICLE"))
    if not found:
        raise SystemExit("EVCC_VEHICLE has no vehicle title")
    return found


# The part before the @ is words with single dots between them, or a text between quotes in which a backslash
# escapes the next character.
ADDRESS = re.compile(r"""([^@\s<>."]+(\.[^@\s<>."]+)*|"([^"\\\r\n]|\\[^\r\n])+")@[^@\s<>.]+(\.[^@\s<>.]+)+""")
# One list item: text up to a comma, semicolon or line end, but a quoted part can hold a comma or semicolon.
LIST_ITEM = re.compile(r"""(?:"(?:[^"\\\n]|\\[^\n])*"?|[^,;\n"])+""")


def addresses(name: str, required: bool = False) -> list[str]:
    """Return the mail addresses of a setting; commas, semicolons and line ends outside quotes separate them."""
    found = [part.strip() for part in LIST_ITEM.findall(env(name, "")) if part.strip()]
    for address in found:
        if not ADDRESS.fullmatch(address):
            raise SystemExit(f"{name}: {address!r} is not a mail address")
    if required and not found:
        raise SystemExit(f"missing configuration: {name}")
    return found


def sender() -> str:
    found = addresses("MAIL_FROM", required=True)
    if len(found) != 1:
        raise SystemExit("MAIL_FROM must be one mail address")
    return found[0]


def smtp_port() -> int:
    text = env("SMTP_PORT", "587")
    if not text.isdigit() or not 1 <= int(text) <= 65535:
        raise SystemExit(f"SMTP_PORT {text!r} is not a port number")
    return int(text)


def run_day() -> int:
    text = env("RUN_DAY", "2")
    if not text.isdigit() or not 1 <= int(text) <= 28:
        raise SystemExit(f"RUN_DAY {text!r} must be a day from 1 to 28")
    return int(text)


def evcc_url() -> str:
    url = env("EVCC_URL")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise SystemExit(f"EVCC_URL {url!r} must be an address such as http://evcc.local:7070")
    return url


def iban() -> str:
    """Return CLAIMANT_IBAN without spaces, after the check of its control number (ISO 13616, mod 97)."""
    value = re.sub(r"\s", "", env("CLAIMANT_IBAN")).upper()
    form = r"NL[0-9]{2}[A-Z]{4}[0-9]{10}" if value.startswith("NL") else r"[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}"
    if not re.fullmatch(form, value):
        raise SystemExit("CLAIMANT_IBAN does not have the form of an IBAN")
    if int("".join(str(int(char, 36)) for char in value[4:] + value[:4])) % 97 != 1:
        raise SystemExit("CLAIMANT_IBAN has a wrong control number; examine it for a typing error")
    return value


def agreed_tariffs() -> list[Tariff]:
    try:
        return parse_tariffs(env("AGREED_TARIFFS"))
    except ValueError as error:
        raise SystemExit(f"AGREED_TARIFFS: {error}") from None


def price_basis() -> tuple[list[Tariff] | None, str]:
    """Return the agreed tariffs (None = the value that evcc recorded) and the agreement reference."""
    basis = env("PRICE_BASIS")
    if basis == "evcc":
        return None, env("AGREEMENT_REF", "")
    if basis != "agreed":
        raise SystemExit("PRICE_BASIS must be 'agreed' or 'evcc'")
    return agreed_tariffs(), env("AGREEMENT_REF")


def billing_zone() -> ZoneInfo:
    return ZoneInfo(env("BILLING_TZ", "Europe/Amsterdam"))


def first_period() -> date | None:
    return parse_period(env("FIRST_PERIOD")) if env("FIRST_PERIOD", "") else None


def action() -> tuple[str, date | None, str]:
    """Return the recovery action of the schedule (ACTION), its month (ACTION_PERIOD) and its id (ACTION_ID).

    A different id makes the same action and month a new request, so the user can ask it again.
    """
    name, ident = env("ACTION", "none"), env("ACTION_ID", "")
    if name not in ACTIONS:
        raise SystemExit(f"ACTION {name!r} must be one of: {', '.join(ACTIONS)}")
    if ident and not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", ident):
        raise SystemExit(f"ACTION_ID {ident!r} must be at most 32 characters of A-Z, a-z, 0-9, '.', '_' and '-'")
    if name == "none":
        return name, None, ident
    try:
        period = parse_period(env("ACTION_PERIOD"))
    except argparse.ArgumentTypeError as error:
        raise SystemExit(f"ACTION_PERIOD: {error}") from None
    if period >= datetime.now(billing_zone()).date().replace(day=1):
        raise SystemExit(f"ACTION_PERIOD {period:%Y-%m} has not ended")
    return name, period, ident


def check_config(mail: bool, scheduled: bool = False) -> None:
    """Stop with one message that lists all the faults in the settings that this run uses."""
    checks = [lambda name=name: env(name) for name in STATEMENT + (MAIL if mail else ())]
    checks += [evcc_url, vehicles, price_basis, billing_zone, iban, output_dir, state_dir]
    if env("PRICE_BASIS", "") == "agreed":
        checks += [agreed_tariffs, lambda: env("AGREEMENT_REF")]
    if mail:
        checks += [smtp_port, sender, lambda: addresses("MAIL_TO", required=True), lambda: addresses("MAIL_CC")]
    if scheduled:
        checks += [lambda: addresses("ALERT_TO"), run_day, first_period, action,
                   lambda: next_run(datetime.now(), env("RUN_AT", "07:00"))]
    faults = []
    for check in checks:
        try:
            check()
        except SystemExit as fault:
            faults.append(str(fault))
        except (ValueError, KeyError, OSError, argparse.ArgumentTypeError) as fault:
            faults.append(f"{type(fault).__name__}: {fault}")
    if faults:
        raise SystemExit("configuration faults:\n- " + "\n- ".join(dict.fromkeys(faults)))


def previous_month(today: date) -> date:
    return date(today.year - 1, 12, 1) if today.month == 1 else date(today.year, today.month - 1, 1)


def parse_period(value: str) -> date:
    if not re.fullmatch(r"20[0-9]{2}-(0[1-9]|1[0-2])", value):
        # argparse shows the text of this error type to the user.
        raise argparse.ArgumentTypeError(f"{value!r} is not a month; use YYYY-MM, for example 2027-01")
    return date(int(value[:4]), int(value[5:]), 1)


def due_periods(today: date, run_day: int, first: date) -> list[date]:
    """Return the months to send on `today`, oldest first.

    The previous month is due from day `run_day` on. Every month from `first` on is due until it is sent, so a
    month that failed until the next month began is not lost.
    """
    last = previous_month(today)
    if today.day < run_day:
        last = previous_month(last)
    periods = []
    while last >= first:
        periods.insert(0, last)
        last = previous_month(last)
    return periods


def next_run(now: datetime, run_at: str) -> datetime:
    if not re.fullmatch(r"([01]?[0-9]|2[0-3]):[0-5][0-9]", run_at):
        raise ValueError(f"RUN_AT {run_at!r} is not a time; use HH:MM, for example 07:00")
    hour, minute = (int(part) for part in run_at.split(":"))
    run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return run if run > now else run + timedelta(days=1)


def schedule() -> None:
    """Run every day at RUN_AT (in BILLING_TZ) from day RUN_DAY of the month on.

    The sent marker makes the runs after a successful send do nothing, so a failed run (evcc down, a session
    still running) is retried the next day.
    """
    check_config(mail=True, scheduled=True)
    day, run_at, zone = run_day(), env("RUN_AT", "07:00"), billing_zone()
    state, output = state_dir(), output_dir()
    first = start_period(state, output, datetime.now(zone).date())
    run_action(state, output, *action())
    while True:  # the first pass is at the start, so a restart after RUN_AT does not skip a day
        run_due(datetime.now(zone).date(), day, first)
        run = next_run(datetime.now(zone), run_at)
        log.info("next run at %s", run.isoformat(timespec="minutes"))
        # Sleep in steps, so a clock change or a suspended host does not shift the run.
        # Elapsed time, not clock time: the two differ in the night that the clock changes.
        while (left := run.timestamp() - time.time()) > 0:
            time.sleep(min(left, 3600))


def run_due(today: date, day: int, first: date) -> None:
    """Do one pass of the schedule: try each due month, and report a failure without a stop of the other months.

    A month with an open correction is due too, also before the start month: a refused resend of an old month
    archived its statement, and nothing else sends the correction.
    """
    periods = due_periods(today, day, first) + open_corrections(state_dir(), output_dir())
    for period in sorted(set(periods)):
        try:
            create_and_send(period)
        except (Exception, SystemExit) as error:
            log.exception("%s failed; next try tomorrow", statement_number(period))
            alert(f"evcc-chargeback: {statement_number(period)} is niet verstuurd",
                  f"{type(error).__name__}: {error}\n")


def clock_goes_back(moment: datetime) -> bool:
    """Return True when the wall-clock time of MOMENT occurs two times, because the clock goes back in its zone.

    Both folds of such a time exist, and the first has the larger offset. A time that does not exist (the clock goes
    forward) also has two offsets, but in the other order.
    """
    local = moment.replace(tzinfo=None)
    first, second = (local.replace(tzinfo=moment.tzinfo, fold=fold).utcoffset() for fold in (0, 1))
    return first is not None and second is not None and first > second


def action_refusal(started: datetime, period: date | None) -> str:
    """Return why an action must not start at STARTED, or "" when it can start. Only the clock decides this.

    run_action decides it on its one clock reading before it stores the request, so a refusal leaves nothing stored
    and the next start does the action. The action itself then tests the same reading and cannot refuse for this.
    """
    stored = "the action did not run and is not stored"
    if clock_goes_back(started):
        # The statement shows the wall-clock time; in this hour a later time can look earlier.
        return (f"the clock in BILLING_TZ goes back in this hour, so the time on a statement can look earlier than "
                f"it is; {stored}; restart the tool after this hour ends")
    if period and period >= started.date().replace(day=1):
        return f"{period_label(period)} has not ended; {stored}; restart the tool when the month has ended"
    return ""


def run_action(state: Path, output: Path, name: str, period: date | None, ident: str = "") -> None:
    """Do the recovery action of the options at most one time, although the option stays set and the app can restart.

    The request goes to the file last-action before the action runs, so no restart repeats it, whatever its result.
    The schedule finishes the work: after a refused send the month is open and the next pass sends it, and after an
    unknown result the .sending marker waits for the user. A request with another ACTION_ID is a new request, so the
    same action can be asked again; a stored request without an id is the same as one with an empty id. ACTION=none
    removes the file.

    The check, the write of last-action and the action hold one lock, so two starts at the same moment cannot both
    do the action.
    """
    request = " ".join(part for part in (name, f"{period:%Y-%m}" if period else "", ident) if part)
    with state_lock(state):
        if name == "none":
            remove_marker("last-action", state, output)
            return
        # One clock reading, under the lock and before last-action is read or written, for the check below, the log
        # line and the statement: a statement of this action never shows an earlier minute than the logged start.
        started = datetime.now(billing_zone())
        refused = action_refusal(started, period)
        if not refused:
            stored = find_marker("last-action", state, output)
            if stored.exists() and stored.read_text().strip() == request:
                log.info("action %s was started at an earlier start and does not run again; check its result in the "
                         "log or the mailbox; to ask it again, change action_id", request)
                return
            remove_marker("last-action", state, output)
            write_atomic(state / "last-action", f"{request}\n".encode())
            try:
                log.info("action %s starts at %s", request, issued_label(started))
                if name == "resend":
                    _create_and_send(period, dry_run=False, force=True, issued=started)
                else:
                    _mark_sent(period)
                failure = None
            except (Exception, SystemExit) as error:
                failure = error
    if refused:
        # Not stored, so the next start does the action.
        log.error("action %s: %s", request, refused)
        alert(f"evcc-chargeback: actie {request} is niet gedaan", f"{refused}.\n")
        return
    if failure is None:
        log.info("action %s done; you can set action to none", request)
        return
    again = ("the action does not run again by itself; before you repeat a resend, look in the mailboxes if the "
             "statement arrived; to repeat the action, change action_id (for example from empty to 2) and restart "
             "the app one time")
    log.error("action %s failed; %s", request, again, exc_info=failure)
    alert(f"evcc-chargeback: actie {request} is mislukt", f"{type(failure).__name__}: {failure}\n\n{again}.\n")


def start_period(state: Path, output: Path, today: date) -> date:
    """Return the first month that the schedule sends: FIRST_PERIOD, else the month before the first start.

    The first start writes that month to the state folder, so a month that fails until the next month begins
    stays due after a restart. Before STATE_DIR was set, that file was in the output folder.
    """
    stored = find_marker("first-period", state, output)
    if not stored.exists():
        write_atomic(stored, f"{previous_month(today):%Y-%m}\n".encode())
    if first := first_period():
        return first
    try:
        return parse_period(stored.read_text().strip())
    except argparse.ArgumentTypeError as error:
        raise SystemExit(f"{stored}: {error}") from None


def alert(subject: str, text: str) -> None:
    """Tell ALERT_TO that a scheduled run failed. A failed alert must not stop the schedule."""
    try:
        if recipients := addresses("ALERT_TO"):
            mailer.send(subject, text, recipients=recipients, **smtp_settings())
    except (Exception, SystemExit):
        log.exception("could not send the alert")


def smtp_settings() -> dict:
    """Read all the mail server settings, so that a fault comes before a send starts."""
    return {"host": env("SMTP_HOST"), "port": smtp_port(), "user": env("SMTP_USER"),
            "password": env("SMTP_PASSWORD"), "sender": sender()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evcc-chargeback", description=__doc__.splitlines()[0])
    parser.add_argument("--period", type=parse_period, help="month as YYYY-MM (default: previous month)")
    parser.add_argument("--dry-run", action="store_true", help="write a draft PDF, do not send it")
    parser.add_argument("--force", action="store_true", help="make and send again when the period was already sent")
    parser.add_argument("--mark-sent", action="store_true",
                        help="record that a send of --period that stopped halfway did arrive; send nothing")
    parser.add_argument("--schedule", action="store_true",
                        help="keep running and send the previous month from day RUN_DAY at RUN_AT")
    args = parser.parse_args(argv)
    if args.schedule and (args.period or args.dry_run or args.force or args.mark_sent):
        parser.error("--schedule does not go together with other options")
    if args.mark_sent and (args.force or args.dry_run):
        parser.error("--mark-sent does not go together with --force or --dry-run")
    if args.mark_sent and not args.period:
        parser.error("--mark-sent needs --period")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_options()

    if args.schedule:
        schedule()
    elif args.mark_sent:
        mark_sent(args.period)
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


def state_dir() -> Path:
    """The folder of the markers that say what was sent. Default: the output folder."""
    state = Path(env("STATE_DIR", "")) if env("STATE_DIR", "") else output_dir()
    state.mkdir(parents=True, exist_ok=True)
    return state


def marker_copies(name: str, state: Path, output: Path) -> list[Path]:
    """Return every copy of a marker: an earlier version also wrote markers to the output folder.

    A folder that is the same as the other one (a link, or STATE_DIR not set) counts once.
    """
    found = {}
    for folder in (state, output):
        if (folder / name).exists():
            found.setdefault((folder / name).resolve(), folder / name)
    return list(found.values())


def find_marker(name: str, state: Path, output: Path) -> Path:
    """Return the marker in the state folder, else the one in the output folder from before STATE_DIR was set.

    A marker that is in neither folder goes to the state folder.
    """
    return next(iter(marker_copies(name, state, output)), state / name)


def remove_marker(name: str, state: Path, output: Path) -> None:
    for copy in marker_copies(name, state, output):
        copy.unlink(missing_ok=True)


def earlier_send(number: str, state: Path, output: Path) -> str:
    """Return the date of the last version of this statement that a recipient can have, or "" if there is none.

    That is the sent marker, or a send that stopped halfway, also when --force already moved the marker away. A
    marker without a date still counts, so a correction always says that it replaces an earlier version.
    """
    markers = [path for suffix in (".sent", ".sending") for path in marker_copies(number + suffix, state, output)]
    if not markers:
        found = {path.resolve(): path for folder in (state, output) for suffix in ("sent", "sending")
                 for path in folder.glob(f"{number}.replaced-*.{suffix}")}
        archived = sorted(found.values(), key=lambda path: path.name)
        # The sent marker of a version comes first, as for the live markers.
        markers = sorted((path for path in archived if path.stem == archived[-1].stem),
                         key=lambda path: path.suffix != ".sent")
    if not markers:
        return ""
    return next((text for path in markers if (text := path.read_text().strip()[:10])), UNKNOWN_DATE)


def open_corrections(state: Path, output: Path) -> list[date]:
    """Return the months whose earlier version was replaced but whose correction is not sent, oldest first.

    Such a month can be before the start month, so the schedule must find it from the markers.
    """
    found = set()
    for folder in (state, output):
        for path in folder.glob("EV-*.replaced-*.s*"):
            if match := ARCHIVED.fullmatch(path.name):
                found.add(parse_period(match[1]))
    return sorted(period for period in found
                  if not any(marker_copies(statement_number(period) + suffix, state, output)
                             for suffix in (".sent", ".sending")))


@contextmanager
def state_lock(state: Path):
    """One run at a time for each state folder. The lock is not reentrant: flock on a second open blocks."""
    with (state / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def create_and_send(period: date, dry_run: bool = False, force: bool = False) -> None:
    """Make the statement of the month and send it."""
    with state_lock(state_dir()):
        _create_and_send(period, dry_run, force)


def mark_sent(period: date) -> None:
    """Make a send that stopped halfway a sent one, after the user saw in the mailbox that the statement arrived."""
    with state_lock(state_dir()):
        _mark_sent(period)


def _mark_sent(period: date) -> None:
    """Nothing is mailed, and the PDF and record stay as they are.

    The PDF is required: without it, a month would be closed with no statement to keep, so the mark is refused. The
    user can always restore it, because it is the attachment of the statement that arrived. The record is not
    required: a delivered correction whose record is lost must not block the month, so a missing record (or
    something that is not a file at its path) only gives a warning.
    """
    number = statement_number(period)
    output, state = output_dir(), state_dir()
    if find_marker(number + ".sent", state, output).exists():
        raise RuntimeError(f"{number} is already sent; there is nothing to mark")
    sending = marker_copies(number + ".sending", state, output)
    if not sending:
        raise RuntimeError(f"{number} has no send that stopped halfway; there is nothing to mark")
    pdf, record = output / f"{number}.pdf", output / f"{number}.json"
    if not pdf.is_file():
        raise RuntimeError(f"{number} has no statement to mark: {pdf.name} is not a file in {output}. If the "
                           f"statement arrived, save its attachment as {pdf.name} in {output} (or restore it from a "
                           f"backup) and mark it again; do not resend, because the recipients then get a second "
                           f"version. Only if no recipient has it, use --period {period:%Y-%m} --force (app: action "
                           f"resend) to make and send it again")
    if not record.is_file():
        log.warning("%s is marked as sent without its record: %s is not a file in %s, so the source data of the "
                    "statement is not kept", number, record.name, output)
    sent_on = next((text for path in sending if (text := path.read_text().strip()[:10])), date.today().isoformat())
    write_atomic(state / f"{number}.sent", f"{sent_on}\n".encode())
    remove_marker(number + ".sending", state, output)
    log.info("marked %s as sent on %s; nothing was mailed", number, sent_on)


def _create_and_send(period: date, dry_run: bool, force: bool, issued: datetime | None = None) -> None:
    """Make the statement of PERIOD and send it. ISSUED is the moment the statement shows; default: now."""
    number = statement_number(period)
    output, state = output_dir(), state_dir()
    zone = billing_zone()
    if force and not dry_run:
        # A correction must show a time with one meaning (see run_action): one clock reading for the check and the
        # statement, before anything is read, archived or sent.
        issued = issued or datetime.now(zone)
        if clock_goes_back(issued.astimezone(zone)):
            raise SystemExit(f"the clock in BILLING_TZ goes back in this hour, so the time on a correction can look "
                             f"earlier than it is; nothing was done; run --period {period:%Y-%m} --force again "
                             f"after this hour ends")
    if not dry_run and period >= (issued or datetime.now(zone)).astimezone(zone).date().replace(day=1):
        raise ValueError(f"{period_label(period)} has not ended; only --dry-run is possible")
    pdf, record = output / f"{number}.pdf", output / f"{number}.json"
    empty, sending, sent = (find_marker(number + suffix, state, output) for suffix in (".empty", ".sending", ".sent"))
    if not dry_run:
        # A fault in the mail settings must come before the marker and before the old statement moves away.
        mail = {"recipients": addresses("MAIL_TO", required=True), "cc": addresses("MAIL_CC"), **smtp_settings()}

    if not dry_run and not force:
        if sent.exists():
            log.info("%s was already sent on %s; use --force to send again", number, sent.read_text().strip())
            return
        if sending.exists():
            raise RuntimeError(f"an earlier send of {number} stopped halfway; look in the mailbox. If it arrived, "
                               f"use --period {period:%Y-%m} --mark-sent (app: action mark_sent). If not, use "
                               f"--period {period:%Y-%m} --force (app: action resend) to send it as a correction")
        # A month with an earlier version has an open correction: a stale .empty marker must not hide it.
        if empty.exists() and not earlier_send(number, state, output):
            log.info("%s had no sessions on %s; use --force to look again", number, empty.read_text().strip())
            return

    tariffs, agreement = price_basis()
    cars = vehicles()
    # Read the connection state first: a car that disconnects after this is in the session list that follows.
    waiting = open_sessions(evcc_url(), period, cars, zone, issued or datetime.now(zone))
    raw = fetch_period(evcc_url(), period)
    problems = []
    if doubtful := doubtful_sessions(raw, cars, names(env("EVCC_IDENTIFIERS", "")), period, zone):
        problems.append(f"sessions {doubtful} have no vehicle, or another vehicle than their RFID tag says; "
                        f"correct the vehicle of these sessions in evcc")
    if waiting:
        problems.append(f"a car has been connected at {', '.join(waiting)} since before the end of the month; "
                        f"its session is complete when it disconnects")
    if problems and env("ALLOW_INCOMPLETE", "false").lower() != "true":
        if not dry_run:
            raise ValueError("; ".join(problems) + " (or set ALLOW_INCOMPLETE=true)")
        log.warning("the draft is not final: %s", "; ".join(problems))
    sessions = select_sessions(raw, cars, period, tariffs, zone)
    log.info("%s: %d sessions of %s", number, len(sessions), ", ".join(cars))
    replaces = earlier_send(number, state, output)
    if not sessions:
        if replaces and not dry_run:
            raise RuntimeError(f"{number} was sent on {replaces}, but evcc now has no sessions of the car in "
                               f"{period_label(period)}; a sent statement cannot be corrected to zero "
                               f"automatically, so settle it by hand")
        log.info("no sessions in %s, nothing to send", period_label(period))
        if not dry_run:
            remove_marker(empty.name, state, output)
            (state / empty.name).write_text(date.today().isoformat() + "\n")
        return
    if not dry_run:
        remove_marker(empty.name, state, output)

    statement = Statement(
        number=number,
        issued=(issued or datetime.now(zone)).astimezone(zone),
        period=period,
        claimant=Party(env("CLAIMANT_NAME"), env("CLAIMANT_ADDRESS", "").replace("\\n", "\n")),
        payer=Party(env("PAYER_NAME"), env("PAYER_ADDRESS", "").replace("\\n", "\n")),
        iban=iban(),
        vehicle_label=env("VEHICLE_LABEL", cars[0]),
        sessions=sessions,
        agreement=agreement,
        fixed_tariff=tariffs is not None,
        replaces=replaces,
    )
    if dry_run:
        draft = render_pdf(statement, output / f"{number}-draft.pdf")
        log.info("wrote %s, total %s; nothing was sent", draft, euro(statement.total))
        return

    # The PDF and its record are final from here: replace them only on --force, and keep the old ones.
    # A send that stopped halfway counts as sent: a recipient can have it. The old marker goes too: until the
    # correction is sent, the month is open again.
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    if markers := marker_copies(sent.name, state, output) + marker_copies(sending.name, state, output):
        for old in (pdf, record, *markers):
            if old.exists():
                old.rename(old.with_name(f"{old.stem}.replaced-{stamp}{old.suffix}"))
    render_pdf(statement, pdf)
    ids = {s.id for s in sessions}
    write_atomic(record, json.dumps({
        "number": number,
        "issued": statement.issued.isoformat(timespec="seconds"),
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

    sending, sent = state / sending.name, state / sent.name
    with sending.open("x") as marker:  # fails when another run sends this statement right now
        marker.write(datetime.now().isoformat(timespec="seconds") + "\n")
    try:
        mailer.send(
            f"{statement.title} {period_label(period)} ({number}{', gecorrigeerd' if statement.replaces else ''})",
            f"Bijgaand de {statement.title.lower()} van {period_label(period)} voor {statement.vehicle_label}.\n\n"
            f"Totaal: {euro(statement.total)}, over te maken naar {statement.iban} o.v.v. {number}.\n",
            attachment=pdf, **mail,
        )
    except mailer.NotSent:
        # The server did not accept the message, so the next run can try again.
        remove_marker(sending.name, state, output)
        raise
    # Every other error keeps the marker: the message may have arrived, so there is no automatic second send.
    write_atomic(sent, f"{date.today().isoformat()}\n".encode())
    remove_marker(sending.name, state, output)
    log.info("sent %s", number)


if __name__ == "__main__":
    sys.exit(main())
