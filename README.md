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

Flagging record for customers with overdue invoices.
Created automatically by the daily scheduler; managed manually by the commercial team.

| Field | Type | Description |
|---|---|---|
| `customer` | Link: Customer | Customer with overdue balance |
| `contract` | Link: Contract | Associated contract |
| `outstanding_amount` | Currency | Total outstanding |
| `overdue_since` | Date | Invoice due date |
| `review_status` | Select | `Pending Review` / `Suspend` / `Reactivate` / `Deactivate` |
| `assigned_to` | Link: User | Commercial staff responsible |
| `notes` | Text | Internal notes |

**Autoname:** `MZ-OVERDUE-.YYYY.-.#####`

**Permissions:** System Manager (full), Accounts Manager (read/write, no delete)

**Deduplication:** the scheduler only creates a new record if no record with `review_status = "Pending Review"` already exists for the same contract.

---

## Contract Custom Fields

Added programmatically via `install.py` (`after_install` / `after_migrate`).
Visible in the **"MozEconomia Cloud"** tab of the Contract form.

| Fieldname | Type | Description |
|---|---|---|
| `ai_saas_tab` | Tab Break | "MozEconomia Cloud" section header |
| `mz_subscription_plan` | Link: Subscription Plan | ERPNext Subscription Plan to use for billing |
| `mz_tenant` | Data | Customer subdomain slug — user types only the prefix (e.g. `boa-construtora`) |
| `mz_tenant_url` | Data (read-only) | Full computed access domain: `<slug>.erp.mozeconomia.co.mz` |
| `contact_email` | Data (Email) | Customer contact email for notifications |
| `mz_linked_subscription` | Link: Subscription (read-only) | ERPNext Subscription created by the lifecycle hook |

### Tenant URL UX

`mz_tenant` is rendered with a visual suffix `.erp.mozeconomia.co.mz` glued to the right of the
input box (via the "AI SaaS - Contract" Client Script). The user types only the slug; the
full domain is immediately visible without requiring a separate field description.

`mz_tenant_url` is updated automatically by the same client script as the user types. It stores
the domain only (no `https://` prefix) — the email template wraps it with `https://`.

### Property Setters

| Field | Property | Value | Reason |
|---|---|---|---|
| `Contract.start_date` | `reqd` | `1` | Subscriptions cannot be created without a start date |

---

## Contract Lifecycle

Handled by `ai_saas.saas.contract_lifecycle`.

### Entry Points

| Event | Hook | Registered In |
|---|---|---|
| `Contract.on_submit` | `on_contract_signed` | `hooks.py` `doc_events` |
| `Contract.on_update_after_submit` | `on_contract_signed` | `hooks.py` `doc_events` |
| `Contract.on_cancel` | `on_contract_cancel` | `hooks.py` `doc_events` |

### Subscription Creation (`on_contract_signed`)

Fires on both `on_submit` and `on_update_after_submit` with the same handler.

**Conditions** — all must be true for action:
- `doc.party_type == "Customer"`
- `doc.is_signed == True`
- `doc.mz_subscription_plan` is set
- `doc.mz_linked_subscription` is empty (not already created)

**Actions:**
1. Resolves default company from user defaults or `Global Defaults`
2. Resolves `Sales Taxes and Charges Template` (default for company)
3. Creates ERPNext `Subscription` with:
   - `party_type = "Customer"`, `party = doc.party_name`
   - `start_date` / `end_date` from Contract
   - `generate_invoice_at = "Beginning of the current subscription period"`
   - `submit_invoice = 1` (auto-submits generated invoices)
   - `days_until_due = 7`
   - `generate_new_invoices_past_due_date = 1`
   - `plans = [{"plan": mz_subscription_plan, "qty": 1}]`
4. Stores the Subscription name in `Contract.mz_linked_subscription`

> **Note:** Subscription status is managed entirely by the ERPNext Subscription controller.
> `ai_saas` does not set or change `review_status` / service status on the Contract.

### Subscription Cancellation (`on_contract_cancel`)

1. Reads `mz_linked_subscription` from the Contract
2. If the linked Subscription exists and its status is not `"Cancelled"`:
   - Calls `sub.cancel_subscription()`
   - Saves the Subscription with `ignore_permissions=True`

### Full Flow

```
Contract (Draft)
  → set mz_subscription_plan, mz_tenant, contact_email, start_date
  → [Sign + Save] → on_contract_signed()
       └── _setup_subscription()
             └── ERPNext Subscription created (SUB-YYYY-xxxx)
             └── mz_linked_subscription saved on Contract
             └── ERPNext auto-generates Sales Invoices on billing cycle start

  → Frappe Notification fires on Contract submit:
       └── "AI SaaS - Boas-Vindas" → email to party_user (CC: Customer.email_id or contact_email)

[ERPNext daily scheduler]
  └── Generates Sales Invoice → auto-submitted (submit_invoice=1)
       └── "AI SaaS - Fatura Emitida" notification → email with attached invoice

[ERPNext notification scheduler — daily]
  ├── "AI SaaS - Lembrete 1" → D-6 before due date (days_in_advance=4 from posting_date)
  ├── "AI SaaS - Lembrete 2" → D-3 before due date (days_in_advance=7 from posting_date)
  ├── "AI SaaS - Lembrete 3" → D-1 before due date (days_in_advance=7 from posting_date)
  ├── "AI SaaS - Escalação Comercial" → internal alert to Sales Manager role
  ├── "AI SaaS - Aviso de Suspensão" → D+1 after due date
  └── "AI SaaS - Aviso de Desativação" → D+8 after due date

[ai_saas daily scheduler]
  └── flag_overdue_customers()
       ├── D-4: pre-billing reminder email (direct sendmail, not a Notification fixture)
       ├── D+1: creates MZ Overdue Review (Pending Review)
       ├── D+7: creates ToDo + Call Event for commercial team
       └── D+15: creates MZ Overdue Review (Deactivate)

[Contract cancel]
  → on_contract_cancel() → Subscription cancelled
```

---

## Email Notifications

Defined as fixtures in `fixtures/notification.json`. All use `channel = "Email"` and `message_type = "HTML"`.

| Name | DocType | Event | Condition | Recipient |
|---|---|---|---|---|
| AI SaaS - Boas-Vindas | Contract | Value Change (`is_signed`) | `is_signed and party_type == 'Customer'` | `party_user` (TO) + `Customer.email_id` or `contact_email` (CC) |
| AI SaaS - Fatura Emitida | Sales Invoice | Submit | `doc.subscription` | `contact_email` (TO) + Role: All |
| AI SaaS - Lembrete 1 | Sales Invoice | Days after `posting_date` (+4) | `subscription and outstanding_amount > 0` | `contact_email` (TO) + Role: All |
| AI SaaS - Lembrete 2 | Sales Invoice | Days after `posting_date` (+7) | `subscription and outstanding_amount > 0` | `contact_email` (TO) + Role: All |
| AI SaaS - Lembrete 3 | Sales Invoice | Days after `posting_date` (+7) | `subscription and outstanding_amount > 0` | `contact_email` (TO) + Role: All |
| AI SaaS - Escalação Comercial | Sales Invoice | Days after `posting_date` (+7) | `subscription and outstanding_amount > 0` | Role: Sales Manager |
| AI SaaS - Aviso de Suspensão | Sales Invoice | Days after `due_date` (+1) | `subscription and outstanding_amount > 0` | `contact_email` (TO) + Role: All |
| AI SaaS - Aviso de Desativação | Sales Invoice | Days after `due_date` (+8) | `subscription and outstanding_amount > 0` | `contact_email` (TO) + Role: All |

### Recipient Strategy

- **Boas-Vindas** uses `receiver_by_document_field = "party_user"` so the Contract creator
  (a Frappe User) receives the welcome email and can access the payment portal link.
  The `cc` Jinja field resolves `Customer.email_id or Contract.contact_email` to also reach
  the customer's external contact email.
- **All billing notifications** use `receiver_by_document_field = "contact_email"` on the
  Sales Invoice (ERPNext natively populates this from the Customer contact) with
  `receiver_by_role = "All"` as a fallback to guarantee delivery.
- **Escalação Comercial** sends only to `receiver_by_role = "Sales Manager"` (internal only).

### `mz_tenant_url` in Welcome Email

The Boas-Vindas template includes:
```html
{% if doc.mz_tenant_url %}
<a href="https://{{ doc.mz_tenant_url }}">{{ doc.mz_tenant_url }}</a>
{% endif %}
```

`doc.mz_tenant_url` stores the domain only (`slug.erp.mozeconomia.co.mz`).
The template prepends `https://` to form the full clickable URL.

### Pre-billing Reminder (D-4)

The `billing_monitor.flag_overdue_customers()` function sends a pre-billing reminder
**4 days before the invoice is generated** via direct `frappe.sendmail()`, not a Notification fixture.
This targets active Subscriptions where `current_invoice_start = today + 4 days`.

---

## Billing Monitor

`ai_saas.saas.billing_monitor.flag_overdue_customers`

Runs daily. Executes the full overdue escalation pipeline in one call:

### 1. Pre-billing Reminder (D-4)

- Finds active Subscriptions where `current_invoice_start = today + 4 days`
- For each, resolves the linked Contract via `mz_linked_subscription`
- Sends a direct email to `contract.contact_email` with:
  - Service period dates
  - Estimated billing amount
  - 7-day payment notice

### 2. Overdue Review Creation (D+1)

- Queries submitted Sales Invoices with `subscription IS NOT NULL`, `outstanding_amount > 0`, `due_date < today`
- For each, resolves Contract via `mz_linked_subscription`
- Creates `MZ Overdue Review` with `review_status = "Pending Review"`
- **Deduplication:** skips if a record with `Pending Review` or `Deactivate` status already exists

### 3. D+7 Follow-up Tasks

- Filters overdue invoices with `days_overdue >= 7`
- For each contract (deduplicated), creates:
  - **ToDo** (High priority, Open) with customer, amount, and days overdue
  - **Event** (Call type, scheduled tomorrow at 09:00) with the same details
- Skips if an open ToDo for the contract already exists

### 4. D+15 Deactivation Queue

- Filters overdue invoices with `days_overdue >= 15`
- Creates `MZ Overdue Review` with `review_status = "Deactivate"` and auto-generated notes
- Skips if a `Deactivate` record already exists for the contract

### Manual Execution

```bash
bench --site erp.local execute ai_saas.saas.billing_monitor.flag_overdue_customers
```

---

## Scheduled Tasks

| Type | Function | Purpose |
|---|---|---|
| `daily` | `ai_saas.saas.billing_monitor.flag_overdue_customers` | Full overdue escalation pipeline |
| ERPNext native daily | `frappe.email.doctype.notification.notification.trigger_daily_alerts` | Fires all "Days Before/After" notifications |

The `trigger_daily_alerts` scheduler is already registered in ERPNext — no additional
scheduler entry is needed in `ai_saas` for the notification-based reminders.

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
