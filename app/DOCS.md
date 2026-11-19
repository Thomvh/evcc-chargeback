# evcc chargeback

This app sends a monthly statement for the home charging cost of a company car. It reads the charging sessions
from evcc and changes nothing in evcc.

Read the full manual: https://github.com/Thomvh/evcc-chargeback

## Configuration

| Option | Meaning |
|---|---|
| `evcc_url` | Address of evcc. For the evcc app: `http://49686a9f-evcc:7070` |
| `evcc_vehicle` | Title of the company car in evcc. After a rename: the two titles with a comma between them |
| `vehicle_label` | Text for the car on the statement |
| `evcc_identifiers` | RFID tags of the company car, with commas between them |
| `price_basis` | `evcc`: the value that evcc recorded. `agreed`: kWh x a fixed tariff |
| `agreement_ref` | Reference to the written agreement, shown on the statement |
| `agreed_tariffs` | Only for `agreed`: start date and price, for example `2027-01-01:0.30` |
| `claimant_name`, `claimant_address`, `claimant_iban` | The person who gets the money. Use `\n` for a new address line |
| `payer_name`, `payer_address` | The company that pays |
| `smtp_host`, `smtp_port`, `smtp_user`, `smtp_password` | Mail server. Port 465 is TLS, other ports use STARTTLS |
| `mail_from`, `mail_to`, `mail_cc` | Sender and recipients of the statement |
| `alert_to` | Gets a message when a run fails |
| `run_day`, `run_at` | The previous month is due from this day, runs are at this time |
| `first_period` | First month to send (`YYYY-MM`). Months from here on are sent until they are done |
| `billing_tz` | Time zone for the month limits. Default: `Europe/Amsterdam` |
| `allow_incomplete` | Send also when a session has no vehicle or a car is still connected |

## Files

The statements and records are in the folder `/share/evcc-chargeback`.
