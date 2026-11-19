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
| `first_period` | First month to send (`YYYY-MM`). Months from here on are sent until they are done. Default: the month before the first start of the app |
| `billing_tz` | Time zone for the month limits. Default: `Europe/Amsterdam` |
| `allow_incomplete` | Send also when a session has no vehicle or a car is still connected |
| `action` | A recovery action that the app does one time only: `none`, `resend` or `mark_sent`. See "Recovery" |
| `action_period` | The month of the action (`YYYY-MM`) |
| `action_id` | Optional free id of the request, for example `2`. A maximum of 32 characters: letters, digits, `.`, `_` and `-`. A different id makes the same action and month a new request. Default: empty |

`run_at` is a time in the time zone `billing_tz`.

Home Assistant keeps the SMTP password with the other options of the app. A person who can read the app
configuration or a backup can read the password. Use a password that is only for this mailbox.

## Files

The statements and records are in the folder `/share/evcc-chargeback`. They are copies for you.

The app keeps the markers that say which months are sent in its own data folder. This folder is part of the
backup of the app. If you install the app again without a restore of its backup, set `first_period` to the next
month that must be sent. If not, the app can send old months again.

## Recovery

A send can stop halfway. For example, the connection to the mail server is lost, or the server accepts the
message for some recipients only. The app then does not know if the statement arrived. It does not send that
month again by itself. Each next run of that month stops with an error, and `alert_to` gets a message.

Look in the mailbox of the recipients. Then do one of these actions:

| Result in the mailbox | Action | What the app does |
|---|---|---|
| All recipients have the statement | `mark_sent` | It records the month as sent and removes the marker of the stopped send. It sends no email. |
| No recipient has the statement | `resend` | It makes the statement again and sends it as a correction. |
| Some recipients have the statement, others do not | `mark_sent` | Confirm who does not have it. Send the PDF by hand first. See the steps below. |

`resend` sends the statement to all recipients. If a recipient already has the statement, that recipient gets
two versions.

Email gives no proof that a message did not arrive. A mail server can keep a message in a queue for hours or days.
Thus you cannot be fully sure that a recipient does not have the statement. Before you send another copy with
`resend` or by hand, do these steps:

1. Ask each recipient to confirm that the statement did not arrive.
2. If you can, look in the queue or the log of the mail server.

After these steps, the first copy can still arrive at a later time. The recipient then has two copies. The two
copies have the same number `EV-YYYY-MM`. A correction from `resend` has the word "gecorrigeerd" in the subject.
Its PDF has a "Vervangt" line. Tell the recipients to use the newest copy.

When some recipients have the statement and others do not, do these steps:

1. Ask each recipient without the statement to confirm that it did not arrive.
2. If you can, look in the queue or the log of the mail server.
3. Find the file `EV-YYYY-MM.pdf` in `/share/evcc-chargeback`.
4. Send this file by email to the recipients who confirmed that they do not have it. Do this by hand.
5. Do the action `mark_sent` with the procedure below.

`mark_sent` needs the PDF of the month in `/share/evcc-chargeback`. If the PDF is not there, the action fails. You
can always restore the PDF, because it is the attachment of the statement that arrived:

- If a recipient has the statement, save its attachment as `EV-YYYY-MM.pdf` in `/share/evcc-chargeback`. You can
  also restore the file from a backup. Then do `mark_sent` again. Do not use `resend`, because the recipients then
  get a second version.
- If no recipient has the statement, use `resend`. Do the two steps above first.

The record (`EV-YYYY-MM.json`) is not necessary for `mark_sent`. If the record is missing, `mark_sent` marks the
month as sent and writes a warning to the log. The source data of that statement is then not kept. If you have a
backup of the record, restore it.

Use `resend` also to send a correction of a month that is already sent.

1. On the Configuration tab, set `action` to the action.
2. Set `action_period` to the month, for example `2026-10`.
3. Save the configuration and restart the app.
4. Read the Log tab. The log shows if the action is done or failed.
5. Set `action` back to `none` and save the configuration.

The app does an action one time only, also when the action fails. Before the action starts, the app stores the
action, the month and the id. If you restart the app with the same action, month and id, it does nothing. The
value `none` clears the stored request and is the normal state.

To do the same action again, change `action_id` (for example from empty to `2`) and restart one time. The app then
does the action first in that start, before the schedule runs.

The app does not do an action in the hour in which the clock goes back in `billing_tz`. It does not store the
request and sends an alert. Restart the app after that hour.

If the app stopped during the action, the log can show no result. The log of the next start then says that the
action "was started at an earlier start and does not run again".

Before you ask for `resend`, always look in the mailboxes if the statement arrived. The markers and the log cannot
tell the result of a send with an unknown result.

After an interrupted `mark_sent`, ask for `mark_sent` again with a new `action_id`. `mark_sent` sends no
statement. If the first try was complete, the second try reports that the month "is already sent".

Do these steps after an interrupted `resend`. Do them also after a `resend` whose log or alert reports an unknown or
a partial result:

1. Look in the mailboxes of the recipients for a statement of the month. Use only a statement that arrived after
   the time of the action.
2. Find out if that statement is the correction. Do this check: the "Datum" line of the statement shows a date
   and a time. A PDF is from the action only when this time is the same as or later than the start time of the
   action. Find the start time of the action here:
   - The log of the action has the line `action resend YYYY-MM starts at DD-MM-YYYY HH:MM`. If you set
     `action_id`, its value comes after the month.
   - If the log does not have this line, use the time when you restarted the app for the action.
   - The times are in the time zone `billing_tz`. No action runs in the hour in which the clock goes back. This
     rule makes sure that each time on a statement of an action has one meaning.

   A statement with an earlier time is an older version. This is also true when it has a "Vervangt" line.

   Then do these steps:
   1. If `EV-YYYY-MM.pdf` is in `/share/evcc-chargeback`, do the check on it. If it fails the check, it is an
      older version. Then the action did not make a correction, and the correction did not arrive. Go to step 4.
   2. If `EV-YYYY-MM.pdf` is missing, do the check on the attachment of the statement from step 1. Do the check
      before you save the attachment.
   3. If the attachment fails the check, it is an older version. Do not save it. Go to step 4.
   4. If the attachment passes the check, save it as `EV-YYYY-MM.pdf` in `/share/evcc-chargeback`.
   5. Compare the statement from step 1 with the PDF in `/share/evcc-chargeback`. The "Datum" line and the "Totaal"
      line must be the same.
3. If the statement from step 1 passed the check of step 2, the correction arrived. Make sure that all recipients
   have it. Ask each recipient without it to confirm that it did not arrive. If you can, look in the queue or the
   log of the mail server. Then send the PDF by hand to the recipients who confirmed this. Then set `action` to
   `mark_sent` and change `action_id`. Save the configuration and restart the app. See the results below this list.
4. If the correction did not arrive, wait for a minimum of one hour. A message can be in transit. Then do steps 1
   and 2 again. One hour is a minimum. It is not a proof that the message did not arrive.
5. If the correction still did not arrive, read the Log tab from the time of the action until now. If the log has
   the line `sent EV-YYYY-MM`, the app sent the correction. Then do steps 1 and 2 again after one more hour. Do not
   ask for `resend`.
6. If the log does not have this line, ask each recipient to confirm that the correction did not arrive. If you
   can, look in the queue or the log of the mail server. Then set `action` to `resend` and change `action_id`. Save
   the configuration and restart the app. The app does the action first in that start. Read the log of that start.
   The line `sent EV-YYYY-MM` means that the work is done. If the log shows an error, read the error in the log or
   the alert. Correct the cause that it names. Then start again at step 1.

A mail server can keep a message in a queue for hours or days. Thus you cannot be fully sure that a correction did
not arrive. After a `resend` or a copy by hand, a recipient can get two copies. The two copies have the same number
`EV-YYYY-MM`. A correction has the word "gecorrigeerd" in the subject and a "Vervangt" line in the PDF. Tell the
recipients to use the newest copy.

In step 3, `mark_sent` gives one of these results. Each result is harmless:

- It marks the month as sent. Then a send stopped halfway. Nothing more is necessary.
- It reports that the month "is already sent". This ends the work only when `EV-YYYY-MM.pdf` in
  `/share/evcc-chargeback` passed the check of step 2. Then the send was complete. If the PDF failed the check,
  the correction did not run. Then go to step 4.
- It reports that the month "has no send that stopped halfway". Then no send is open. Nothing more is necessary.

`mark_sent` can also report that the month "has no statement to mark". Then the PDF is missing in
`/share/evcc-chargeback`. You can always restore it, because it is the attachment of the statement that arrived.
Save that attachment as `EV-YYYY-MM.pdf` in `/share/evcc-chargeback`. Then ask for `mark_sent` again with a new
`action_id`. The record (`EV-YYYY-MM.json`) is not necessary. Without it, `mark_sent` writes a warning that the
source data is not kept.

`alert_to` gets a message when an action fails. The app does not do the action again by itself. If the mail server
refused the correction, the month has an open correction. Each run tries the open correction again. This is also
true for a month before `first_period`. A "no sessions" marker (`.empty`) does not close an open correction. If evcc
has no sessions of the car for that month any more, each run reports an error for that month. The run continues
with the other due months. The app does not correct a sent statement to zero automatically. Settle that statement by
hand. If the result of the send is not known, look in the mailbox again.

A second method is a command in the app container. You must have shell access to the host. Find the container
name with `docker ps`. Then run, for example, `docker exec addon_<repository>_evcc_chargeback evcc-chargeback
--period 2026-10 --mark-sent` or the same command with `--force` in place of `--mark-sent`.
