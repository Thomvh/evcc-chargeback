"""Render the monthly cost refund statement as a PDF (Dutch labels, invoice-style layout)."""

from dataclasses import dataclass
from xml.sax.saxutils import escape
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .sessions import Session, total_cost, total_energy

MONTHS_NL = [
    "januari", "februari", "maart", "april", "mei", "juni",
    "juli", "augustus", "september", "oktober", "november", "december",
]


FOOTER_COMMON = (
    "De kWh per sessie komen van de meter in het laadpunt, zoals vastgelegd door evcc. De meterstanden zijn de "
    "tellerstanden van het laadpunt aan het begin en het eind van de sessie. Dit document is geen factuur; er "
    "wordt geen btw in rekening gebracht."
)
FOOTER_AGREED = "Het bedrag per sessie is het aantal kWh maal het overeengekomen tarief. " + FOOTER_COMMON
FOOTER_EVCC = (
    "Het bedrag per sessie is de waarde zoals vastgelegd door evcc: netstroom tegen het nettarief van "
    "dat moment, eigen zonne- en batterijstroom tegen het teruglevertarief. " + FOOTER_COMMON
)


@dataclass(frozen=True)
class Party:
    name: str
    address: str = ""  # lines separated by "\n"


@dataclass(frozen=True)
class Statement:
    number: str
    issued: datetime  # the moment the statement was made, aware, in the billing time zone
    period: date
    claimant: Party
    payer: Party
    iban: str
    vehicle_label: str
    sessions: list[Session]
    agreement: str = ""  # reference to the written agreement between the parties; empty = a plain cost claim
    fixed_tariff: bool = False  # True: kWh times the agreed tariff; False: the value that evcc recorded
    replaces: str = ""  # send date of the earlier version that this statement corrects

    @property
    def title(self) -> str:
        return "Afrekening laadstroom" if self.agreement else "Declaratie laadkosten"

    @property
    def basis(self) -> str:
        if self.fixed_tariff:
            return f"Tarief per kWh volgens {self.agreement}"
        value = "Waarde per laadsessie zoals vastgelegd door evcc"
        return f"{value}, volgens {self.agreement}" if self.agreement else value

    @property
    def total(self) -> Decimal:
        return total_cost(self.sessions)

    @property
    def energy(self) -> Decimal:
        return total_energy(self.sessions)


def statement_number(period: date) -> str:
    return f"EV-{period:%Y-%m}"


def issued_label(moment: datetime) -> str:
    """Date and time to the minute, as on the statement and in the log line of a recovery action."""
    return f"{moment:%d-%m-%Y %H:%M}"


def period_label(period: date) -> str:
    return f"{MONTHS_NL[period.month - 1]} {period.year}"


def nl_number(value: Decimal, places: int) -> str:
    """Format with a decimal comma and a thousands dot, as in Dutch documents."""
    text = f"{value:,.{places}f}"
    return text.replace(",", "_").replace(".", ",").replace("_", ".")


def euro(value: Decimal) -> str:
    return f"€ {nl_number(value, 2)}"


def _party_block(label: str, party: Party, style) -> Paragraph:
    lines = [party.name, *[line for line in party.address.split("\n") if line]]
    return Paragraph("<br/>".join([f"<b>{label}</b>", *map(escape, lines)]), style)


def render_pdf(statement: Statement, path: Path) -> Path:
    styles = getSampleStyleSheet()
    body = styles["BodyText"]
    small = styles["BodyText"].clone("small", fontSize=8, leading=10, textColor=colors.grey)

    doc = SimpleDocTemplate(
        str(path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
        title=f"{statement.title} {statement.number}", author=statement.claimant.name,
    )
    story = [Paragraph(f"{statement.title} auto van de zaak", styles["Title"])]

    parties = Table(
        [[_party_block("Van", statement.claimant, body), _party_block("Aan", statement.payer, body)]],
        colWidths=[87 * mm, 87 * mm],
    )
    parties.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [parties, Spacer(1, 6 * mm)]

    meta = Table(
        [
            ["Nummer", statement.number],
            ["Datum", issued_label(statement.issued)],
            ["Periode", period_label(statement.period)],
            ["Auto", statement.vehicle_label],
            ["Grondslag", Paragraph(escape(statement.basis), body)],
            *([["Vervangt", f"de versie die op {statement.replaces} is verzonden"]] if statement.replaces else []),
        ],
        colWidths=[30 * mm, 144 * mm],
    )
    meta.setStyle(TableStyle([("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [meta, Spacer(1, 6 * mm)]

    rows = [["Datum", "Start", "Einde", "Laadpunt", "Meter begin", "Meter eind", "kWh", "€/kWh", "Bedrag"]]
    for s in statement.sessions:
        rows.append([
            f"{s.start:%d-%m-%Y}",
            f"{s.start:%H:%M}",
            f"{s.end:%d-%m %H:%M}" if s.end.date() != s.start.date() else f"{s.end:%H:%M}",
            s.loadpoint,
            "" if s.meter_start is None else nl_number(s.meter_start, 1),
            "" if s.meter_stop is None else nl_number(s.meter_stop, 1),
            nl_number(s.energy_kwh, 3),
            nl_number(s.price_per_kwh, max(4, -s.price_per_kwh.normalize().as_tuple().exponent)),
            euro(s.cost),
        ])
    rows.append(["Totaal", "", "", f"{len(statement.sessions)} sessies", "", "", nl_number(statement.energy, 3), "",
                 euro(statement.total)])
    table = Table(rows, colWidths=[20 * mm, 11 * mm, 21 * mm, 28 * mm, 21 * mm, 20 * mm, 17 * mm, 15 * mm, 21 * mm],
                  repeatRows=1)
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.black),
        ("LINEABOVE", (0, -1), (-1, -1), 0.75, colors.black),
        ("ALIGN", (4, 0), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f2f2f2")]),
    ]))
    story += [table, Spacer(1, 8 * mm)]

    story.append(Paragraph(
        f"Graag <b>{euro(statement.total)}</b> overmaken naar <b>{escape(statement.iban)}</b> "
        f"t.n.v. {escape(statement.claimant.name)}, o.v.v. <b>{escape(statement.number)}</b>.", body))
    story += [Spacer(1, 6 * mm), Paragraph(FOOTER_AGREED if statement.fixed_tariff else FOOTER_EVCC, small)]

    doc.build(story)
    return path
