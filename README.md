### AI SaaS


Frappe app that manages SaaS marketing, customer contracts and billing for MozEconomia Cloud.
Depends on `erpnext` and `erpnext_mz`.

---

## Table of Contents

1. [Installation](#installation)
2. [DocTypes](#doctypes)
3. [Contract Custom Fields](#contract-custom-fields)
4. [Contract Lifecycle](#contract-lifecycle)
5. [Email Notifications](#email-notifications)
6. [Billing Monitor](#billing-monitor)
7. [Scheduled Tasks](#scheduled-tasks)
8. [File Structure](#file-structure)
9. [Verification & Testing](#verification--testing)

---

## Installation

```bash
# 1. Ensure the Python package is installed in the bench virtualenv
./env/bin/pip install -e apps/ai_saas

# 2. Add to the bench apps list
echo "ai_saas" >> sites/apps.txt

# 3. Install on the site
sudo -u erp-user bench --site erp.local install-app ai_saas

# 4. Migrate (creates tables, syncs custom fields, installs client scripts)
sudo -u erp-user bench --site erp.local migrate

# 5. Clear cache
sudo -u erp-user bench --site erp.local clear-cache
```

**Required apps (must be installed first):** `erpnext`, `erpnext_mz`

To re-apply setup after code changes without a full migrate:

```bash
bench --site erp.local execute ai_saas.install.after_migrate
```

---

## DocTypes

### `MZ Overdue Review`

The one place a human decides on an account, and its audit trail. Created by the daily billing scheduler
for overdue invoices, from the Contract form (button **Rever conta**) for a manual suspend/reactivate,
or by the customer from `/reactivar?contract=&token=` (`api/reactivation.request`) after a suspension
or archive email. Setting `review_status` executes through `tenant_lifecycle`, which reports the new
stage to the Opportunity.

| Field | Type | Description |
|---|---|---|
| `customer` | Link: Customer | Customer with overdue balance |
| `contract` | Link: Contract | Associated contract |
| `outstanding_amount` | Currency | Total outstanding |
| `overdue_since` | Date | Invoice due date |
| `origin` | Select | `Facturação` (engine) / `Manual` (Contract button) / `Pedido do Cliente` (`/reactivar`) |
| `review_status` | Select | `Pending Review` / `Suspend` / `Reactivate` / `Deactivate` |
| `assigned_to` | Link: User | Commercial staff responsible |
| `notes` | Text | Internal notes |

**Autoname:** `MZ-OVERDUE-.YYYY.-.#####`

**Permissions:** System Manager (full), Accounts Manager (read/write, no delete), Sales Manager (create/write)

**Deduplication:** the scheduler only creates a new record if no record with `review_status = "Pending Review"` already exists for the same contract.

---

## Contract Custom Fields

Defined in `install.py` (`_sync_custom_fields`, applied on `after_install` / `after_migrate`) and mirrored in
`fixtures/custom_field.json`. Visible in the **"MozEconomia Cloud"** tab of the Contract form.

| Fieldname | Type | Description |
|---|---|---|
| `mz_saas_tab` | Tab Break | "MozEconomia Cloud" tab |
| `mz_subscription_plan` | Link: Subscription Plan | Plan to bill. **Editable after submit** until a Subscription is linked (the customer may correct it at activation) |
| `mz_tenant` | Data | Customer subdomain slug — the user types only the prefix (e.g. `boa-construtora`) |
| `mz_tenant_url` | Data (read-only) | Full access domain: `<slug>.erp.mozeconomia.co.mz` |
| `contact_email` / `mz_contact_name` / `mz_contact_mobile` | read-only `fetch_from` | Mirrors of the Customer (`email_id`, `customer_primary_contact`, `mobile_no`) — the primary **Contact** is the source; nothing writes these directly |
| `mz_linked_subscription` | Link: Subscription (read-only) | Subscription created at signature |
| `mz_apps_to_install` | Table: MZ Tenant App | Apps installed on the tenant (defaults from `provisioning.DEFAULT_APPS`) |
| `mz_billing_start` | Date (read-only) | The day billing actually began (later of `start_date` and the signature date); the post-contract notifications anchor here |

`Lead.mz_segment` (Link: Segment Intelligence Map) carries the industry captured at signup.

There is no stored account phase: `tenant_lifecycle.account_phase(contract)` derives it — no provisioning
row → `""` (invisible to the engine), provisioning `Archived` → Closed, `Suspended` → Suspended,
`is_signed` → Active, else Trial. `live_trials()` is the one query the engine, the usage probe and the
signup ceiling share.

`Opportunity.mz_stage_since` (Datetime) and `Opportunity.mz_signup` (Link) are the two fields the
remarketing campaigns read — see *The Funnel*.

### Tenant URL UX

`mz_tenant` is rendered with a visual suffix `.erp.mozeconomia.co.mz` glued to the right of the input
(Client Script "AI SaaS - Contract"); `mz_tenant_url` is computed as the user types and stores the domain only.

### Property Setters

| Field | Property | Value | Reason |
|---|---|---|---|
| `Contract.start_date` | `reqd` | `1` | It is the trial end and the billing anchor |

---

## The Funnel

The full design is in `docs/sales-funnel.md` (business rationale) and `docs/sales-funnel-implementation.md`
(itemized, code-verified plan with a per-row review of what was built). In one paragraph:

`/registo` step 1 already creates the Lead and the Opportunity (stage `Cloud - Form Started`); finishing
the form creates Customer (group `Cloud - Trial`), primary Contact, Billing Address and an **unsigned,
submitted Contract** (stage `Cloud - Account Created`). Submission provisions the tenant site
(trial begins, no Subscription). A daily probe reads usage from the trial site and raises hot/cold lead
signals; countdown emails anchor on `Contract.start_date`. `/activar` signs the contract — through the
document save path, so the hooks run — which creates the Subscription with billing starting on the later
of `start_date` and the signature date. If nobody signs, the lifecycle engine suspends the site on
`start_date`, and archives it (full backup, then `drop-site`) after the grace period. Overdue invoices
suspend 33 days after their due date. **Nothing counts days; everything reads dates.**

**The Opportunity is the funnel's ledger.** Every lifecycle event reports its `sales_stage` through
`crm.report()`, which stamps `mz_stage_since`: Form Started → Account Created → Trial Engaged / At Risk
(probe) → Activated (signature, *Converted*) · Trial Expired / Suspended (engine or review) →
Closed (archive, *Lost*). Campaigns are Notifications on Opportunity, `Days After mz_stage_since`:
G1 unfinished signups, G2 expired trials, G3 closed accounts. The contact lives in the Lead until
there is a Customer, then in the Customer's primary Contact — Opportunity and Contract only mirror it.

### Two triggers, split along the signature (`ai_saas.saas.contract_lifecycle`)

| Event | Handler | Does |
|---|---|---|
| `Contract.on_submit` | `on_contract_submitted` | Queues provisioning regardless of signature, creates the Subscription only if already signed |
| `Contract.on_update_after_submit` | `on_contract_signed` | Acts only on the `is_signed` 0→1 transition: Subscription, Opportunity → Activated/Converted, customer moved out of the trial group; guards the plan against changes once a Subscription exists |
| `Contract.on_cancel` | `on_contract_cancel` | Cancels the linked Subscription |

Subscription: `generate_invoice_at = "Beginning of the current subscription period"`, `submit_invoice = 1`,
`days_until_due = 7`, `generate_new_invoices_past_due_date = 0`. When billing starts today, the first
invoice is issued immediately (`sub.process(today)`) — ERPNext only generates on an exact date match.

### Modules

| Module | Role |
|---|---|
| `api/signup.py` | Guest endpoints behind `/registo`: `start`, `update`, `check_subdomain`, `submit`, `status`. Token-only access; one live signup per email; duplicates refused generically at submit |
| `saas/activation.py` + `www/activar` | Token-gated activation page and transaction (`get_activation_url` is exposed to Jinja) |
| `saas/provisioning.py` | Site creation (`provision_tenant`), retry of `Failed` records, delivery email from the `MozEconomia Cloud - Entrega da Conta` Email Template |
| `saas/tenant_lifecycle.py` | `suspend` / `reactivate` / `archive` and the daily engine (`process_lifecycle`) — **unarmed by default** |
| `saas/usage_signals.py` + `erpnext_mz.utils.tenant_usage` | Daily read-only probe of trial sites (the probe ships in erpnext_mz, the app tenants have) → `MZ Tenant Usage Snapshot` → hot/cold signals on the Opportunity |
| `saas/billing_monitor.py` | Pre-billing reminder, D+1 overdue review row, commercial follow-up ToDo + call Event |
| `saas/crm.py` | The funnel ledger: `STAGES`, `report` / `report_for_contract` / `touch`, Opportunity resolution, sales ToDos |
| `api/reactivation.py` + `www/reactivar` | Token-gated customer reactivation request → `MZ Overdue Review` (origin *Pedido do Cliente*) |
| `install.py` | Custom fields, Contract Template, Email Template, Calendly booking link (`MZ SaaS Settings.booking_url`), trial Customer Group, sales stages, legacy-form retirement |

### DocTypes

| DocType | Role |
|---|---|
| `MZ SaaS Settings` (Single) | Trial length, dry-run switch, suspension/grace/reminder thresholds, ops alert recipients, default sales user, commercial customer group, self-service ceilings |
| `MZ Signup` | One row per signup in progress (resume token, step timestamps, links to the documents created) |
| `MZ Tenant Provisioning` | One record per contract for life: status incl. `Suspended` / `Archived`, `suspended_on`, `backup_path`, log |
| `MZ Tenant Usage Snapshot` | One row per trial per day from the probe |
| `MZ Overdue Review` | Dunning review queue; `Suspend` / `Reactivate` / `Deactivate` **execute** through `tenant_lifecycle` |

---

## Email Notifications

Fixtures in `fixtures/notification.json` — 32 records, all `AI SaaS - *`. None sends to a bare role.

| Group | Records | Anchor | Recipient |
|---|---|---|---|
| G1 unfinished-signup nurture | `Lead Nurture - Dia 0` (on New), `Dia 3/10/20` (Days After `mz_stage_since`) | Opportunity, `sales_stage == "Cloud - Form Started"` | `contact_email`; every body carries the resume link via `mz_signup` |
| G2 expired trials | `Trial Expirado - Dia 1/7/21` (Days After `mz_stage_since`) | Opportunity, `Cloud - Trial Expired` | `contact_email`; activation link — signing is the reactivation |
| G3 closed accounts | `Conta Encerrada - Dia 3/30` (Days After `mz_stage_since`) | Opportunity, `Cloud - Closed`, only while a `backup_path` exists | `contact_email`; `/reactivar` link, retention from `archive_retention_days` |
| Trial countdown | `Trial - 7 dias`, `- 3 dias`, `- Último dia amanhã`, `- Hoje` (Days Before `start_date`) | Contract, unsigned | `contact_email`; copy branches on the latest usage snapshot; activation + booking links |
| Invoice + dunning | `Fatura Emitida`, `SMS Fatura Emitida`, `Lembrete 1/2/3` (+1/+4/+7 from `posting_date`), `Aviso de Suspensão` (+1 from `due_date`), `Aviso de Desativação` (+30 from `due_date`), `Recibo de Pagamento` — every one conditioned on `doc.is_return == 0` | Sales Invoice / Payment Entry | `contact_email` (+ billing contacts in CC) |
| Credit note | `Nota de Crédito` (on Submit, `doc.is_return == 1 and not doc.is_pos`), `Nota de Crédito (MZ)` attached | Sales Invoice | `contact_email` (+ billing contacts in CC) |
| Internal | `Escalação Comercial` (+7) | Sales Invoice | role Sales Manager |
| Post-contract | `Pós-Contrato Dia 3/5/33` (Days After `mz_billing_start`) | Contract, signed | `contact_email` |
| Booking | `Marcação Confirmada` (on New) | Appointment | `customer_email` |

Every customer-facing email names only actions and dates the engine will actually take: suspension is
**33 days after the due date**, never "nas próximas horas".

**Two delivery paths.** Notifications always go through the Email Queue, which only the scheduler's
`frappe.email.queue.flush` sends — a site with the scheduler disabled shows them `Not Sent` with no error.
`frappe.sendmail(..., delayed=False)` mails (delivery, lifecycle, "já tem uma conta") bypass the queue.
`bench --site <site> scheduler status` is the first check when a Notification "is not sent".

The delivery (welcome) email is not a Notification: `provisioning._send_welcome_email` renders the
`MozEconomia Cloud - Entrega da Conta` Email Template (trial copy with activate/book links, or billing copy
when the contract was already signed).

---

## Billing Monitor

`ai_saas.saas.billing_monitor.flag_overdue_customers` — daily. Thresholds come from `MZ SaaS Settings`.

1. **Pre-billing reminder** (`prebilling_reminder_days` before `current_invoice_start`, default 4) — direct email to `contact_email`.
2. **Overdue review row** — one `MZ Overdue Review` (`Pending Review`) per contract per overdue invoice.
3. **Commercial follow-up** (`overdue_followup_days`, default 7) — High ToDo + call Event, one open ToDo per contract.

Suspension and archive are **not** here: `tenant_lifecycle.process_lifecycle` reads dates and acts.

```bash
bench --site erp.local execute ai_saas.saas.billing_monitor.flag_overdue_customers
bench --site erp.local execute ai_saas.saas.tenant_lifecycle.process_lifecycle   # honours dry-run
```

---

## Inventory of legacy accounts

Before touching accounts the old funnel created, look at them:

```bash
bench --site erp.mozeconomia.co.mz execute ai_saas.saas.legacy_migration.inventory
```

Walks every tenant site directory (`sites/`, `archived/sites/`), asks each live site who it
belongs to and how it is used (`erpnext_mz.utils.tenant_usage.identity` — Company, NUIT, its
System Managers, invoices, logins, size), and matches the control site's Customers, Contracts,
Subscriptions, Opportunities and Leads to it by site name, NUIT, email, mobile and company name —
each match labelled with the key that produced it, conflicts listed, never guessed. The result
(`tenant_inventory_<date>.xlsx`: sheets `sites`, `control_only`, `summary`) is attached to
**MZ SaaS Settings** for download; the `class` column (`paying`, `debtor_live`, `used_unsigned`,
`never_used`, `archived_by_hand`, `unmatched_site`, `unclassified`) is a hint for reading, not a
decision. Read-only apart from that File; re-running replaces the day's file. Actions come after.

---

## Scheduled Tasks

| Type | Function | Purpose |
|---|---|---|
| `daily` | `ai_saas.saas.billing_monitor.flag_overdue_customers` | Reminders, review rows, follow-ups |
| `daily` | `ai_saas.saas.tenant_lifecycle.process_lifecycle` | Trial expiry → suspend; 33 days overdue → suspend; grace elapsed → archive (dry-run until switched off) |
| `daily` | `ai_saas.saas.usage_signals.collect_usage_snapshots` | Probe trial sites, hot/cold signals |
| `daily` | `ai_saas.multipay.tasks.sync_pending_payments` | Multipay reconciliation |
| `hourly` | `ai_saas.saas.provisioning.retry_stuck_provisioning` | Re-queue stuck provisioning |
| ERPNext native daily | `trigger_daily_alerts` | Fires every "Days Before/After" notification (pinned to 08:00 by `install.ensure_daily_alerts_hour`) |
| Frappe native, every minute | `frappe.email.queue.flush` | Sends the Email Queue — every Notification depends on it |

---

## File Structure

```
apps/ai_saas/
├── FEATURES.md                              # This file
├── pyproject.toml                           # Project metadata, ruff config
└── ai_saas/
    ├── __init__.py                          # __version__ = "1.0.0"
    ├── hooks.py                             # App metadata, doc_events, scheduler, fixtures
    ├── install.py                           # after_install / after_migrate setup
    ├── modules.txt                          # "AI SaaS"
    ├── config/
    │   └── __init__.py
    ├── fixtures/
    │   ├── custom_field.json                # Contract custom fields (export artifact)
    │   ├── notification.json                # 8 email notification templates
    │   └── client_script.json              # "AI SaaS - Contract" client script (export artifact)
    ├── ai_saas/                             # Frappe module "AI SaaS"
    │   ├── __init__.py
    │   └── doctype/
    │       └── mz_overdue_review/
    │           ├── mz_overdue_review.json   # DocType definition
    │           └── mz_overdue_review.py     # class MZOverdueReview(Document): pass
    └── saas/
        ├── contract_lifecycle.py            # Subscription create/cancel hooks
        └── billing_monitor.py              # Daily overdue escalation pipeline
```

### Key Design Decisions

1. **Custom fields via `install.py`**, not only via fixture JSON. `create_custom_fields(..., update=True)`
   is idempotent and runs on every migrate. The `custom_field.json` fixture is the export artifact.

2. **Client script via `install.py`**. The "AI SaaS - Contract" script is created/updated
   programmatically so the source of truth is Python code, not a fixture JSON string.
   It is also exported via the `Client Script` fixture filter.

3. **`_STALE_FIELDS` dict** in `install.py` lists fields from previous iterations that are
   explicitly deleted on every migrate, preventing orphaned columns.

4. **`mz_tenant_url` stores domain only** (no `https://`). The Notification template adds
   the protocol when rendering the hyperlink.

---

## Verification & Testing

### After Installation

```bash
# Verify custom fields on Contract
bench --site erp.local execute frappe.db.get_all \
  --args '["Custom Field", {"dt": "Contract", "module": "AI SaaS"}, ["fieldname", "label", "read_only"]]'

# Verify notifications imported
bench --site erp.local execute frappe.db.get_all \
  --args '["Notification", [["name", "like", "AI SaaS%"]], ["name", "enabled", "event"]]'

# Verify client script created
bench --site erp.local execute frappe.db.exists \
  --args '["Client Script", "AI SaaS - Contract"]'
```

### Manual Test Flow

```
1. Create Customer + Contract (party_type=Customer)
   → Fill: mz_subscription_plan, mz_tenant (e.g. "test-co"), start_date
   → Observe: mz_tenant_url auto-populates to "test-co.erp.mozeconomia.co.mz"
   → Sign (is_signed=1) and Save
   → Verify: mz_linked_subscription is filled
   → Verify: Subscription exists in ERPNext Accounts
   → Verify: "AI SaaS - Boas-Vindas" in Email Queue

2. Simulate overdue pipeline:
   bench --site erp.local execute ai_saas.saas.billing_monitor.flag_overdue_customers
   → Verify: MZ Overdue Review records created for overdue invoices

3. Cancel contract:
   → Cancel the Contract document
   → Verify: linked Subscription status = Cancelled
```

### Export Fixtures After UI Changes

After modifying Notifications or the Contract Client Script via the ERPNext UI:

```bash
sudo -u erp-user bench --site erp.local export-fixtures --app ai_saas
```
