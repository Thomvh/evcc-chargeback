# evcc-chargeback

Send a monthly statement for the home charging cost of a company car, from the session data of
[evcc](https://evcc.io).

The tool is for this situation: you charge a company car at home, on your private energy contract, and evcc
controls the charger. The company pays you back each month. The tool makes the statement and the evidence for it.

The statement is in Dutch. Other languages are not available.

## What the tool does

1. It reads the finished charging sessions of the previous month from the evcc API.
2. It selects the sessions of the company car.
3. It calculates the amount of each session (see "Price basis").
4. It makes a PDF with the session list, the meter readings and the total amount.
5. It sends the PDF by email to the company.
6. It keeps the PDF and a record file with the source data of evcc.

The tool only reads from evcc. It sends GET requests to `/api/sessions` and `/api/state` and changes nothing.

## Rules

- A session belongs to the month in which it starts, in the time zone `BILLING_TZ`.
- The energy of a session is the value that evcc recorded from the meter of the charge point, rounded to Wh.
- The total is the sum of the rounded session amounts.
- The tool sends a statement one time only. To send a correction, use `--force`.
- The tool sends a statement only for a month that has ended.
- The tool does not send a statement when the month is not complete. See "When the tool stops".

## Price basis

Set `PRICE_BASIS` to one of these values. Ask your accountant which basis is correct for you.

| Value | Amount of a session | Title of the document |
|---|---|---|
| `agreed` | kWh x the tariff of the start day, from `AGREED_TARIFFS` | Afrekening laadstroom |
| `evcc` with `AGREEMENT_REF` | The value that evcc recorded for the session | Afrekening laadstroom |
| `evcc` without `AGREEMENT_REF` | The value that evcc recorded for the session | Declaratie laadkosten |

`agreed` is for a written agreement between you and the company with a fixed price per kWh. Put the reference to
the agreement in `AGREEMENT_REF`. The statement shows it. Add a new entry to `AGREED_TARIFFS` when the price
changes: `2027-01-01:0.31,2028-01-01:0.29`.

`evcc` with `AGREEMENT_REF` is for a written agreement that makes the value from evcc the price: grid energy at
the grid price of that moment, own energy at the feed-in price. The statement shows the reference.

`evcc` uses the cost that evcc calculates. evcc prices grid energy at the grid tariff of that moment. It prices
energy from your solar panels and your home battery at the feed-in tariff. This is not the full cost price per kWh
of your energy contract.

For the Netherlands, read the positions of the Belastingdienst before you choose: KG:204:2024:13, KG:204:2024:14
and KG:204:2026:16 (kennisgroepen.belastingdienst.nl). This tool does not give tax advice.

## Vehicle selection

evcc stores the vehicle title in each session. Set `EVCC_VEHICLE` to the title of the company car. If you renamed
the vehicle in evcc, give the two titles, with a comma between them.

Make sure that evcc knows which car charges:

- Give each car its own RFID tag and add the tag to the vehicle in evcc (`identifiers`).
- Do not make the company car the default vehicle of a charge point that other cars use.
- Set `EVCC_IDENTIFIERS` to the RFID tags of the company car. The tool then finds sessions that started with such
  a tag but have a different vehicle.

You can correct the vehicle of a finished session in the evcc user interface (Sessions page).

## When the tool stops

The tool does not send the statement, and tries again on the next day, when:

- evcc does not answer.
- A session of the month has no vehicle, or has a different vehicle than its RFID tag says.
- A car has been connected to a charge point since before the end of the month. evcc stores a session only when
  the car disconnects.
- A session has no cost in evcc (price basis `evcc` only).
- The mail server refuses the connection, the login or the message.

Set `ALERT_TO` to get an email when a scheduled run fails. Set `ALLOW_INCOMPLETE=true` to ignore the vehicle and
connection checks.

The tool stops and does not try again by itself when:

- A send stopped halfway (the file `EV-YYYY-MM.sending` exists). Look in the mailbox. Then use `--force` to send
  again, or rename the file to `EV-YYYY-MM.sent`.
- The mail server accepted the message for some recipients only.
- The connection to the mail server failed during the send. The result is not known.

## Files

The tool writes these files to `OUTPUT_DIR`. Keep them with your records. Use one output directory for each car.

| File | Contents |
|---|---|
| `EV-YYYY-MM.pdf` | The statement that was sent |
| `EV-YYYY-MM.json` | The record: session lines, total, SHA-256 of the PDF and the source sessions from evcc |
| `EV-YYYY-MM.sent` | The date of the send |
| `EV-YYYY-MM.empty` | The month had no sessions of the car. Use `--force` to look again. |
| `EV-YYYY-MM.replaced-<time>.*` | An earlier statement that `--force` replaced |
| `EV-YYYY-MM-draft.pdf` | The result of `--dry-run`. This file is not a statement. |

evcc permits changes to a finished session, also without a login. The record file shows the data at the time of
the statement.

## Commands

```sh
evcc-chargeback --period 2026-10 --dry-run   # make a draft PDF for October 2026, send nothing
evcc-chargeback                               # make and send the previous month
evcc-chargeback --period 2026-10 --force      # make and send October again, as a correction
evcc-chargeback --schedule                    # stay active and send each month (the container default)
```

## Schedule

With `--schedule` the tool stays active. It does a run when it starts, and then each day at `RUN_AT`. The previous
month is due from day `RUN_DAY`. After a successful send, the next runs do nothing for that month.

Set `FIRST_PERIOD` to the first month that the tool must send. The tool then also sends older months that are not
sent yet. Without `FIRST_PERIOD`, the tool only tries the previous month.

Do not set `RUN_AT` between 02:00 and 03:00. The change to and from summer time makes this hour unreliable.

## Installation as a container

1. Build the image: `docker build -t evcc-chargeback:0.2.0 .`
2. Copy `deploy/compose.yaml` to a directory outside this repository.
3. Copy `.env.example` to `.env` in that directory and fill in the values, also the SMTP password.
4. Make `.env` readable for you only: `chmod 600 .env`.
5. Start the stack: `docker compose up -d`.
6. Do a test: `docker compose exec evcc-chargeback evcc-chargeback --period 2026-10 --dry-run`.

The container must be able to connect to evcc. Use a private network (for example a VPN) or HTTPS. The evcc
session API has no login, so do not make it available on the internet.

## License

MIT. See `LICENSE`. This project is not part of evcc.

## Development

```sh
uv venv && uv pip install -e '.[test]'
.venv/bin/pytest
```

## Configuration

Give each value as an environment variable. See `.env.example` for all values. Keep credentials out of this
repository.

Each value can also come from a file: give the path in the `_FILE` variant of the variable. For example, use
`SMTP_PASSWORD_FILE=/run/secrets/smtp_password` with a Docker secret if you do not want the password in `.env`.
