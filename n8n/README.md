# n8n: delivery and follow-up

Praman decides what goes to whom and which step comes next. n8n delivers it and keeps the clock.

```
She approves a letter, ticks "send on my behalf"
  -> POST /api/case/{id}/draft/{id}/send            (backend checks approval + consent, redacts, signs)
  -> n8n webhook "Letter from Praman"
       -> Which insurer adapter?  (email | portal | none yet)   <- edit this to add an insurer
       -> send, with 3 retries
       -> POST /api/n8n/delivered   backend marks it sent and starts the clock on that step's window
          (or POST /api/n8n/failed  backend puts it back to approved)
       -> Wait until the day after respond_by
       -> POST /api/n8n/clock-due   backend's ladder answers: escalate | ask_agent | wait | stop
          escalate / ask_agent -> email the agent (and she gets a WhatsApp message from the backend)
          stop (resolved, deleted, consent withdrawn) -> the workflow ends
```

The workflow holds no rules and no dates. Windows come from `backend/data/ladders/escalation_steps.yaml`
through the backend, so a window changes in one place.

## Set up (n8n Cloud)

1. Redeem the hackathon voucher (see the organizer's PDF; do not commit it).
2. Import `praman-delivery.workflow.json` (Workflows -> Import from file).
3. Credentials:
   - **Praman secret**: Header Auth, name `x-praman-secret`, value = `N8N_SECRET` from `backend/.env`.
     Used by the webhook and by every HTTP node that calls back.
   - **SMTP for demo letters**: any SMTP account. Set the `toEmail` of *Send by email* to a **test inbox you own**
     and the agent address in *Alert the agent*. Never point it at a real insurer from a demo.
   - *Send to insurer portal*: set its URL to a test endpoint (for example a webhook.site address).
4. Publish the workflow and copy the **production** webhook URL into `backend/.env` as `N8N_DISPATCH_URL`.
5. n8n Cloud cannot reach `localhost`. Run a tunnel to the backend (for example
   `cloudflared tunnel --url http://localhost:8000`) and put its address in `PUBLIC_BASE_URL`.
6. `backend/.env`: `N8N_SECRET=<same secret>`; restart the backend.

## Adding an insurer

Open *Which insurer adapter?*, add a rule on `body.addressee` (contains the insurer's name), and point it at a
send node: email, an HTTP call to a portal or API, or a copy of either. No backend deploy.
Until a rule matches, the letter goes to *No adapter yet* and the backend puts it back to approved
with the reason on the case.

## Demo without waiting two weeks

On the backend set `N8N_DEMO_DAYS_AHEAD=15` and, in n8n, change *Wait for the response window* to
"After time interval, 20 seconds". The backend then treats the window as ended, the ladder picks the next step,
and the agent email arrives. Set it back to `0` and the real calendar applies. The windows themselves are still
UNVERIFIED (see `verify_report.py`); the console and case page say so.

## What crosses the line to n8n

Only: case id, draft id, letter kind, addressee (the insurer's legal name), the letter with account, Aadhaar, PAN
and policy numbers masked, the step name and its window in days, and the callback address. No phone number, no
session id, no document text, no file names. n8n Cloud is a third party, so the backend sends nothing without the
case's `contact_insurer` consent, and "Delete everything" stops the workflow at its next check.

## Honest wording

"Sent" means n8n reported the letter went out (email accepted by the SMTP server, or the portal call returned OK).
It does not mean the insurer received or accepted it, and nothing here is called filed.

## Premium reminders (second workflow)

`praman-reminders.workflow.json`: she asks the chat to remind her about her premium, confirms the due date and agrees to the
email. Praman works out the days (`REMINDER_DAYS_BEFORE`, default 7 and 1 days before; never a day that has passed) and
hands them to this workflow with the case id, her email and the due date, and nothing else.

```
POST /webhook/praman-reminder  (x-praman-secret)
  -> one item per date
  -> wait until 09:00 IST on that date
  -> POST /api/n8n/reminder-due   backend allows it only while the case, her consent and that reminder are in force
       send: true  -> email her ("may be due", never "overdue" or "paid")
       send: false -> stop (cancelled, replaced, consent withdrawn, case deleted, already sent)
```

Set up: import the file, attach the same **Praman secret** and **SMTP account** credentials, set the sender in
*Email the reminder*, publish, and put the production webhook URL in `backend/.env` as `N8N_REMINDER_URL`.
Until the chat asks her for an email, `REMINDER_EMAIL` in `backend/.env` is the recipient. Leave either blank and reminders are off.
She can say "stop reminders" at any time; "Delete everything" also stops them at the next wake-up.
