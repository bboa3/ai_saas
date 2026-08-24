# Sales Funnel Restructure

> **Prerequisite:** `../../erpnext_mz/docs/improvements.md` — specifically Part B1, "Company creation". This plan assumes a provisioned site arrives fully configured and able to invoice. Without that, self-service signup only moves the barrier instead of removing it.

## 1. Why this exists

The current funnel promises a no-commitment trial, but the account only comes into existence after a conversation with sales. A prospect fills in a form expecting to get into the product and is met with a request for a phone call. People who evaluate software in the evening or at weekends are never answered. And the moment the purchase decision is actually made — when the first invoice arrives — nobody is talking to them.

We are replacing this with a funnel where **the account creates itself and the customer decides after using it**:

```
Single form on the landing page  ->  account created automatically  ->  trial period with real use
->  the customer signs (activates), or the account is deactivated with a backup
```

The eight problems this addresses, and what resolves each:

| Problem | Resolution |
|---|---|
| 1. No way to try the product without talking to someone | The contract is created submitted but unsigned, and the site is provisioned anyway |
| 2. The promise and the mechanism don't match | The 7 nurture emails go; the new cadence tracks a trial that actually exists |
| 3. Demand and availability are in opposite time zones | Provisioning is automatic — it works at 11pm on a Saturday |
| 4. The moment of conversion has no touch at all | The cadence tightens as the contract start date approaches |
| 5. Two forms for the same purpose | The second is retired in the same act that creates the first |
| 6. Qualification rests on opinion, not behaviour | A daily read of the customer's site (invoices issued, logins) plus commercial alerts |
| 7. Stalled leads get no treatment | Three remarketing segments with automatic triggers |
| 8. Waiting depends on human availability | Both waits disappear: automatic entry, activation by a click |

## 2. How the new structure works

```
External landing page (single form, multi-step, resumable, plan selected here)
   |- guest API  ->  signup record
            v submission
      Lead + Customer + Contract SUBMITTED BUT UNSIGNED
      (plan · subdomain · contract start date)
            v submission triggers provisioning, exactly as it does today
      Customer site live · on trial · NO Subscription
            v daily usage probe -> commercial alerts
            v cadence anchored on the contract start date
   "Activate"  ->  signed page: confirm plan and billing details
                   ->  contract signed  ->  Subscription + first invoice
   "Book a call"  ->  scheduling with a chosen time, inside the system
            v contract start date arrives with no signature
      Suspension  ->  login blocked, data intact
            v + grace
      Archive  ->  backup + destruction  ->  reactivation segment
```

Once active: invoice due in 7 days · unpaid 33 days after due date -> suspension · + grace -> archive. **The 40-day rule, without exception.**

### The two dates that govern everything

This plan does not rest on a trial duration. It rests on two dates:

| Date | What it is | Where it lives |
|---|---|---|
| **Contract start date** | The day the trial ends and billing should begin. Set when the contract is created. | `Contract.start_date`, native field |
| **Billing start** | The day the subscription begins billing: the later of the contract start date and the signature date. | Computed at signature, passed to the Subscription |

The trial period is simply the interval between the contract's creation and its start date. How many days that is — or whether it exists at all — is irrelevant to the design: **nothing in the system counts days, everything reads dates.**

All activation messaging uses the contract start date as its reference, and the same date triggers suspension if nobody signs.

## 3. Where this lives

Bench at `/srv/frappe/frappe-bench`, control site `erp.local`, app `apps/ai_saas`. Customer sites are their own Frappe sites, created with `bench new-site`, at `<subdomain>.erp.mozeconomia.co.mz`. `bench` commands run as `erp-user`.

**Code this plan touches:**

| File | What it does today |
|---|---|
| `ai_saas/hooks.py` | Registers the Contract `doc_events` and the scheduled jobs |
| `ai_saas/saas/contract_lifecycle.py` | On Contract submit: creates the Subscription and requests provisioning — both gated on `is_signed` |
| `ai_saas/saas/provisioning.py` | Creates the customer site (`bench new-site`, setup wizard, welcome email). Single entry point: `provision_tenant(contract_name)` |
| `ai_saas/saas/site_helpers.py` | Code executed *inside* the customer's site, invoked from the control site |
| `ai_saas/saas/billing_monitor.py` | Daily dunning job: reminders and review records |
| `ai_saas/fixtures/notification.json` | The 20 `AI SaaS - *` notifications (nurture, dunning, post-contract) |

**Relevant DocTypes:**

| DocType | Role |
|---|---|
| `Contract` (ERPNext) | The contract. Native fields used: `start_date`, `is_signed`, `status`, `party_name`. Our fields in the *MozEconomia Cloud* tab: plan, subdomain, contact email, linked subscription |
| `MZ Tenant Provisioning` | One record per site created: status, log, attempts. Currently requires a Contract |
| `MZ Overdue Review` | The dunning review queue. Has Suspend/Reactivate/Deactivate states that **do nothing** |
| `Subscription` (ERPNext) | Created on signature; this is what bills |
| `Lead Onboarding` + Web Form `lead-onboarding-form` | The second form, to be retired |
| `Appointment` + `Appointment Booking Settings` (ERPNext) | Native scheduling, currently unused — replaces the Calendly link |

**Named things this plan changes or deletes:** the notifications `AI SaaS - Lead Nurture - Dia 0/3/5/10/15/20/30` (the "7 nurture emails"), the notification `AI SaaS - Aviso de Desativação` (today it fires 8 days after the due date, when real deactivation moves to day 33), and the Calendly link embedded in the body of `AI SaaS - Lead Nurture - Dia 10`.

## 4. Settled decisions

- **The contract remains the origin of provisioning, and is still submitted on creation.** Exactly one thing changes: it no longer starts out signed. Submitted but unsigned = site on trial, no subscription. **Signing is activating.**
- The trigger does not change — it is submission, as it is today. Nothing starts firing on a draft save, which would be unpredictable.
- **No date field is created.** Trial start is the contract's creation date; trial end is `Contract.start_date`. Both already exist.
- A submitted contract with `is_signed = 0` gets `status = "Unsigned"` — native ERPNext behaviour that serves us as is.
- Since a contract always requires a customer, the Customer is created at the start of the trial. **Consequence to watch:** every trial produces a Customer that may never buy, and trial customers must be distinguishable from real ones or they will distort sales reporting.
- One dedicated site per customer, as today.
- Deactivation in two steps: block access -> after grace, back up and destroy.
- The form lives on the external landing page and calls guest endpoints in this app.
- **Nothing is written inside the customer's site.** Trial communication happens by email and SMS.
- **The trial does not use ERPNext's Subscription trial mechanism.** The native controller pushes the billing period past the trial, requires an exact date match to generate the first invoice (one missed scheduler run loses the period), and holds the subscription in `Trialling` — a state the pre-billing warnings filter out. During the trial there is no Subscription at all; there is an unsigned contract.

## 5. Sequence of work

Phases are ordered by **dependency**, not by theme, around one milestone: **the day self-service opens to the public**. Everything that prevents losing money or data comes before that day.

The rule that sets the order: never open the front door before the exit (deactivation) and the till (activation) exist. Otherwise you accumulate sites nobody switches off, and customers who want to pay and can't.

The current sales path — the team creating an already-signed contract by hand — must keep working through every phase, without exception.

```
BLOCK A · Engine              1 · Unsigned contract provisions
(invisible to customers)      2 · Suspend, reactivate, archive
                              3 · Usage signals

BLOCK B · Customer flow       4 · Single form
                              5 · Activation
                              6 · Trial communication

MILESTONE ------------------  Open self-service to the public

BLOCK C · Recovery            7 · Remarketing
```

Each phase below states **the result** — what becomes true — and **how you know** it is done.

---

## BLOCK A — Engine

Three phases with no customer-visible effect. By the end of the block, a trial contract created by hand produces a complete, measured trial that switches itself off on the right date. It is the whole funnel working in the lab, before any public form exists.

### Phase 1 — An unsigned contract provisions

**Result:** what is today one trigger becomes two, separated by the signature:

- **contract submitted** -> the site is created and the trial begins, signed or not;
- **contract signed** -> the subscription is created and billing begins.

Today both are tied to `is_signed` in `contract_lifecycle.on_contract_signed`. Separating them is the change that carries the whole funnel — and it is a small one, because the moment provisioning fires stays exactly what it is today: submission.

An unsigned contract never has an active subscription, and signing it later does not create a second site — the `MZ Tenant Provisioning` record is the same one from beginning to end.

**No date field is created.** Trial start is the contract's creation date; trial end is `start_date`, set at creation, and it is also the reference for all activation messaging and for automatic suspension.

Two things ERPNext imposes: `end_date` stays empty (with it set and `start_date` in the future, the native status would become "Inactive"), and the trial contract is generated from a **Contract Template**, because `contract_terms` is mandatory and is the text the customer accepts on signing.

**The plan field must stay editable after submission.** It is the one field a customer may legitimately want to change at the moment of signing, and a submitted contract freezes its fields by default. NUIT and address do not have this problem — they live on the Customer and the Address, not on the contract.

The contract also gains one new field, in the *MozEconomia Cloud* tab, showing the account's phase:

| Phase | Meaning |
|---|---|
| **Trial** | Submitted unsigned, site running, no billing |
| **Active** | Signed, subscription billing |
| **Suspended** | Access blocked, data intact, reversible |
| **Closed** | Site archived with backup |

**This field is the only thing that marks a contract as a trial**, and it is what the Phase 2 engine reads to decide what it may suspend. A commercial contract that happens to be unsigned is never touched — that distinction must exist before any code is allowed to switch accounts off.

**Provisioning goes back to being provisioning.** It knows how to create a site and report its state — nothing more. It holds no commercial deadlines and no business rules.

**How you know:** submit an unsigned contract with a subdomain and the site is created, with no subscription attached; sign it later and the subscription appears with **no second site**. Open the contract and see, in one place, the account phase and the date the trial ends. Creating an already-signed contract the old way still produces exactly today's result.

*Files:* `saas/contract_lifecycle.py`, `saas/provisioning.py`, phase field on Contract.

### Phase 2 — Accounts switch themselves off without losing data

**Why now:** every trial is a site with its own database. Opening the entrance before the exit exists means accumulating sites nobody switches off. This comes before any trial can arrive unattended.

**Result:** the business rules start executing. Today `billing_monitor` writes an `MZ Overdue Review` with status "Deactivate" and nothing else happens — that DocType's controller is empty.

There are two steps, always in this order. **Block:** access is cut, the data stays exactly where it is, and one click restores everything — this is what makes the reactivation offer credible. **Archive:** after the grace period, a full backup is taken, the site is destroyed, and where the copy went is recorded.

Two rules trigger this:

- **the contract start date arrived and it is still unsigned** — the trial ended without converting;
- **an invoice unpaid 33 days past its due date.**

The team can bring either forward or reverse it by hand from the review queue, whose states stop being decorative.

The engine **reads dates, it does not count days**: it reads `Contract.start_date` and the invoice due date. Changing the trial length affects only trials created afterwards; those already running keep the date they were created with. The remaining deadlines — the 33 days and the grace period before destruction — are configurable in one place, alongside the switch that runs the engine in dry-run mode.

The CRM follows: a trial that expires unsigned marks the opportunity lost and leaves the contract unsigned and idle. The Customer and the data stay, because they are the raw material of the reactivation campaign. Reactivating within the grace period restores everything, including a contract ready to sign.

Customer warnings start matching what will actually happen — today `AI SaaS - Aviso de Desativação` fires 8 days after the due date, 25 days before deactivation is even scheduled.

**How you know:** a trial whose start date has passed stops accepting logins the next day, and accepts them again the moment it is reactivated; after the grace period the site is gone from the server and the backup is on disk. All of it runs in **dry-run** first, recording what it would do, before the switch is turned on.

*Files:* new `saas/tenant_lifecycle.py`, a new configuration Single, the `MZ Overdue Review` controller, dunning notifications.

### Phase 3 — Qualification rests on facts

**Why now:** without measurement the funnel opens blind — we would not know whether trials are being used or why they fail. And this is where the information that makes Phase 6's messaging conditional comes from.

**Result:** knowing whether a lead is good no longer requires a phone call. Every day the system reads from each trial account what happened there — invoices issued, the date of the first one, users created, last login — and brings it into the CRM. Read-only; nothing in the customer's site is altered. The read uses the same mechanism `provisioning.py` already uses to run code inside a customer site.

Two alerts come out of it, and both are worth money: **hot lead** the moment a first invoice is issued — the strongest signal there is, they used the product for what it is for — and **cold lead** midway through the trial with no login and no invoice, while there is still time to recover it.

**How you know:** issue an invoice on a trial account and, the next day, find the opportunity marked as engaged and the task in the sales queue.

*Files:* `saas/site_helpers.py` (the probe), new `saas/usage_signals.py`, a new snapshot DocType.

---

## BLOCK B — Customer flow

What the customer sees and does: they enter, they decide, and they are accompanied. By the end of this block the funnel is ready to open.

### Phase 4 — One form, interruptible and resumable

**Result:** the landing page has **one** form collecting everything needed to open the account — company details, NUIT, VAT regime, contacts, subdomain and **the chosen plan**. In the same act, `lead-onboarding-form` is retired: no more double collection, no more barrier mid-funnel.

It is filled in steps, and anyone who leaves halfway — to go find the NUIT, because the phone rang — gets a link and returns exactly where they stopped, on another device if need be, with no account and no password. Note for whoever implements it: Frappe Web Forms **do not support resuming** (there is no autosave, and a Guest can never reopen an existing document), so this behaviour has to be built.

Anyone who already has an account is detected and **stopped**: if the email or the NUIT already exist, the system neither opens a second account nor reveals anything about the existing one — the landing page gets a generic error and the team gets a notification to handle the case. That is what the email confirmation is for: catching duplicates, not acting as a barrier.

On submission, with nobody from the team involved, the Lead, the Customer and the unsigned contract are created — from the Contract Template, with plan, subdomain and start date — and its submission triggers provisioning. The landing page follows the progress instead of leaving the person in the dark.

Anyone who starts and does not finish **is recorded** — that record feeds the hottest remarketing segment in the funnel, which today does not exist at all.

**When the site fails to come up**, and it will, the promise cannot be left hanging: the customer is told the account is taking longer than expected and the team gets an alert naming the case. The contract and the Customer already exist, so the request is resumed, not restarted.

**How you know:** fill in half the form, close the browser, open the link on another computer and find the fields filled; finish and receive the email with the account address and credentials, with no human involved. Repeat with the same NUIT and get an error, with the team notified.

*Files:* a new signup DocType, new `ai_saas/api/signup.py`, `hooks.py` and the old form's fixtures.

### Phase 5 — Activating is signing the contract

**Result:** the customer converts on their own, at any hour, and the conversion is literally the signing of the contract that has existed since day one.

The link leads to a page that **asks for no new choices**: it shows the plan they already chose on the landing page and the billing details collected in the form, and asks only for confirmation — with the option to correct the plan, the NUIT, the address or the contact before signing.

**The billing rule — signing early never costs money:**

| Situation | Billing starts on |
|---|---|
| Signs before the contract start date | **The contract start date** — they keep the trial time they had left |
| Signs after that date | **The signature date** |

In other words: always the later of the two. Someone who signs early keeps using it free until the end of the period and only then gets the first invoice — which removes the incentive to defer the decision to the last day, precisely when accounts get lost to forgetfulness.

On confirmation the contract is signed and the existing hook chain does the rest: the subscription is created and the first invoice issued as soon as the period begins, without waiting for the daily job.

The site is the same one: nothing is migrated, nothing is lost, the customer does not log in anywhere new.

**How you know:** sign before the start date and confirm billing only begins on that date; sign after it and confirm it begins the same day. In both cases the subscription attaches to the same contract and no new site is created.

*Files:* new page `ai_saas/www/activar.py|html`.

### Phase 6 — The messaging starts telling the truth

**Result:** in a single move, the seven `AI SaaS - Lead Nurture - Dia N` emails — which spend 30 days asking someone who cannot activate anything to "activate your account" — disappear, and the real trial communication takes their place. There is no moment when both exist.

The communication has two anchors, and neither counts trial days:

- **From the contract's creation** — delivery of the account and first steps. The delivery email stops being a generic welcome: address, credentials, how long they can try it, and both paths always present — activate, or book a call.
- **Counting down to the contract start date** — the messages that create the decision. As the date approaches they come closer together and get more explicit about what happens if nothing is done, right up to the day itself. It is this countdown, not a fixed calendar, that creates urgency.

Thanks to Phase 3, each message speaks differently to someone who has already invoiced and to someone who has not even logged in.

The second call to action stops being the Calendly link embedded in `Dia 10`: scheduling moves to ERPNext's native `Appointment`, with real available times, email confirmation and automatic consultant assignment. The lead stops requesting contact and starts knowing when they will be seen.

**How you know:** no email promises anything the recipient cannot do at the moment they receive it; changing a trial contract's start date shifts the entire countdown with it; the customer's site stays exactly as delivered.

*Files:* `fixtures/notification.json`, `saas/provisioning.py` (delivery email), `Appointment Booking Settings` configuration.

---

## MILESTONE — Open self-service to the public

Not a development phase, a decision. Before the switch is flipped:

- **Workers**: today `background_workers = 1`, and creating a site occupies the `long` queue for minutes, blocking everything else. This has to increase before more than one request can arrive at once.
- **Ceilings**: a limit on concurrent trials and on signups per day, plus a per-IP rate limit on the form.
- **The deactivation engine for real**: it leaves dry-run only after the log confirms, over several days, that it identifies exactly the right accounts.
- **A full end-to-end rehearsal** with a throwaway subdomain (see Verification).
- **Company creation fixed in `erpnext_mz`** — Part B1 of `../../erpnext_mz/docs/improvements.md`. Self-service is only worth opening if the instance the customer receives is ready to use.

---

## BLOCK C — Recovery

### Phase 7 — Remarketing for the three segments

**Result:** the three segments that do not exist today come into being, each with its own automatic trigger:

- **Incomplete form** — anyone who started and did not finish gets a single message with the link to resume where they stopped. It is the hottest segment in the funnel, and the cheapest to recover.
- **Unconverted leads** — those who had an account and never used it enter periodic campaigns, using ERPNext's native `Email Campaign`.
- **Deactivated accounts** — those who used it and left get a concrete offer: the data is backed up and the account comes back exactly as it was.

**How you know:** a form abandoned yesterday produces a message today with a resume link that works; an archived account appears in the reactivation list with its backup locatable.

*Files:* remarketing jobs in `hooks.py`, native ERPNext campaigns.

---

## 6. Verification

1. **Block A in the lab:** trial contract created by hand -> site responding -> probe returning real numbers -> move `start_date` back to yesterday -> engine in dry-run -> engine for real -> login blocked -> reactivate -> login restored -> archive -> backup on disk and site removed.
2. **Activation:** repeat the trial and sign, once before `start_date` and once after — confirm in both cases that **no second site is created**, that the subscription attaches to the contract, and that billing starts on the correct date. Also confirm that changing the plan at the moment of signing works.
3. **Messaging:** change a trial contract's `start_date` and confirm the whole countdown moves with it.
4. **Signup:** walk the whole flow from the command line before a landing page exists; then with the landing page, including interrupting and resuming on another device, and the duplicate-NUIT case.
5. **Regression of the current path:** create an already-signed contract the old way and confirm it produces exactly today's result.
6. **Automated tests** for signup, provisioning from an unsigned contract, site lifecycle and activation. Mind the known pattern in this bench: `doc.submit()` commits, so `tearDown` cleanup must be explicit — `frappe.db.rollback()` is not enough.
7. `erp.local` and the real site `saas.erp.mozeconomia.co.mz` untouched by any test.

## 7. What not to build

- Data migration between sites — the trial site is the final site.
- The trial on top of ERPNext's native Subscription trial mechanism.
- Provisioning triggered by a draft save — the trigger is submission, as it is today.
- Counting days anywhere — the system reads dates.
- Any write inside the customer's site to communicate about the trial — communication is by email and SMS.
- Consuming `AI N8N Configuration` — it points at an email address and no code reads it.
