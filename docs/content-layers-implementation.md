# Content layers — implementation

> **Business rationale:** `plano-materializacao-conteudo.md` (Portuguese draft, 2026-09-06). This file is the code-verified counterpart, the same pairing as `sales-funnel.md` / `sales-funnel-implementation.md`. Built 2026-09-06.

## Context

The funnel starts at `/registo`. A company not ready to buy had no place in the system: no list, no permanent presence, no reason to remember MozEconomia on the day it needs certified software. Three layers now sit in front of the form — a free fiscal-deadline alert (builds the list), a sector guide with a two-email sequence (creates the recognition moment), and rare auxiliary emails (keeps the contact warm) — and every new customer can be traced to the layer that brought it.

## One state machine, front to back

`saas/crm.py::STAGES` is the whole journey. Four stages were added in front of `Cloud - Form Started`; nothing after it changed. Every stage is entered by an observed event, and what a contact receives is a function of the stage alone. Nothing is set by hand.

| Stage | Means | Entered by | Receives |
|---|---|---|---|
| `Cloud - Subscribed` | In the base, guide delivered, no signal yet | `/alerta-fiscal` (`api/content.subscribe`); Lead button **Enviar guia sectorial** (`content.enroll`) | Day-0 guide, Day-4 offer, Alerta (if flagged) |
| `Cloud - Aware` | Opened the guide page | the guide link (`api/content.guide_opened`) from Subscribed or Dormant | Day-4 (if still due), auxiliary emails, Alerta |
| `Cloud - Dormant` | No signal through N auxiliary emails | `content.send_auxiliary` when `mz_aux_sent` reaches `MZ SaaS Settings.auxiliary_max_without_signal` | Alerta only |
| `Cloud - Unsubscribed` (Lost) | Asked to leave | `/sair` → `content.suppress` | nothing |
| `Cloud - Form Started` … | existing funnel | `/registo` — `crm.enter_crm` **reuses** the Open Opportunity at Subscribed / Aware / Dormant, reports Form Started and resends Dia 0 (a stage change fires no "New" event) | existing sequences |

"Interested" is not a stage: interest is an activated trial (`Cloud - Account Created` → `Cloud - Trial Engaged`). The draft's "Reciclagem" (a hand-set return date) has no observable trigger and was not built; Dormant plus a guide-link open covers the return. `crm.report()` resets `mz_aux_sent` on every stage change, so any signal restarts the auxiliary count.

The **Alerta Fiscal is not stage-gated**: it goes to every Lead with `mz_fiscal_alerts = 1` and `unsubscribed = 0`, whatever the stage — a customer on trial or paying still wants the deadline.

## Data

- **Lead** (fixture `custom_field.json`): `mz_fiscal_alerts` (Check), `mz_tax_regime` (Select, the four `MZ Signup.tax_regime` options). Native `unsubscribed`, `source`, `company_name`, `mz_segment` reused.
- **Opportunity**: `mz_guide_sent_on` (Date — the Day-4 anchor), `mz_guide_opened_on`, `mz_guide_last_opened` (Datetime), `mz_aux_sent` (Int). Native `source` reused.
- **Web Page**: `mz_segments` (Table MultiSelect of `MZ Guide Segment` → Segment Intelligence Map): the sectors a guide serves. It lives on the page, not on the segment, because the segment map is an exported fixture and Frappe's fixture import validates links — a Web Page name missing on another site would roll back all 17 segments.
- **`MZ Fiscal Obligation`** (new DocType): `title`, `code` (matches `Conformidade MZ.conformidade`: IVA, IRPS, INSS, IRPC, E-DECLARACAO), `periodicity` (Monthly / Quarterly / Annual), `deadline_day`, `deadline_month`, `applies_to_all_segments`, `requires_vat`, `who`, `what_to_submit`, `legal_basis`, `alert_subject`, `alert_body` (Jinja: `deadline`, `lead`, `obligation`), `reviewed_by`, `reviewed_on`, `enabled`, `last_alerted_deadline` (read-only). `validate` refuses `enabled` without both review fields. `install.ensure_fiscal_obligations` seeds five rows **disabled**, deadline rules left for the accountant; never touches an existing row.
- **MZ SaaS Settings**: `holiday_list` (default `Feriado Moçambicano`), `fiscal_alert_lead_days` (3), `auxiliary_max_without_signal` (3), `meta_pixel_id`. Read through `saas/settings.get_settings()` (tuple and return both extended).
- **Lead Source** rows (`install.ensure_lead_sources`): Alerta Fiscal, Guia Sectorial, Importação — stamped on `Lead.source` (first origin only) and `Opportunity.source`, so "where did each new customer come from" is one filter on native fields.

## Layer 1 — Alerta Fiscal (`saas/fiscal_alerts.py`)

- `next_deadline(obligation, today)`: monthly → the day this or next month; quarterly → `deadline_month` + every 3 months; annual → month/day this or next year; a day past the month's end clamps to the last day.
- `utils/dates.py::subtract_business_days(date, n, holiday_list)`: walks back skipping weekends and Holiday List dates. Weekends are always skipped, so a list without a weekly off cannot turn Saturday into a business day. If the list does not cover the current year, one ops alert per year (`_warn_if_uncovered`) and weekends-only counting continue.
- `send_due_alerts(today, dry_run)` — daily (`hooks.scheduler_events`, pinned to 08:00 by `install.ensure_daily_alerts_hour`, which now pins `MORNING_JOBS`). For each **enabled and reviewed** obligation: `deadline`, `alert_date = deadline − lead_days`; if `alert_date ≤ today ≤ deadline` and `last_alerted_deadline != deadline`, one `frappe.sendmail(reference_doctype="Lead")` per eligible Lead (flagged, not unsubscribed, segment carries the code unless `applies_to_all_segments`, regime not Isento when `requires_vat`), then the stamp. **Window + stamp, not an exact-date match**: the existing `Days After` sequences lose a touch when the scheduler skips a day; a fiscal deadline cannot.
- The body is the obligation's fixed text plus the footer that names MozEconomia Cloud as certified software and carries the `/sair` link — nothing else. The alert never sells.

## Layer 2 — the sector guide (`saas/content.py`)

- The guide is a **native Web Page** the team writes and publishes; the page's `mz_segments` table says which sectors it serves; the review date is typed in the page. Print CSS goes in the page's `css` field.
- `guide_url(opportunity)` = the page's route + `?o=<opportunity>&t=<hmac>`, the same HMAC `activation.py` uses (`erpnext_mz.qr_code.qr_generator`). No stored token. `''` when the segment has no published guide. Exposed to Jinja as `mz_guide_url`, with `mz_unsubscribe_url`.
- `send_guide(opportunity)` — Day 0: Email Template `MozEconomia Cloud - Guia Sectorial` (sector in the subject, three lines from the page's `meta_description`, the link, nothing else), `delayed=False`, stamps `mz_guide_sent_on`. Silent when there is no published guide.
- `public/js/guide_open.js` (hooks `web_include_js`, inert without `?o=&t=`) posts to `api/content.guide_opened`, which validates the token and calls `record_guide_open`: stamps first/last open; Subscribed / Dormant → Aware; Aware → `touch()` and the auxiliary count reset; past the form nothing changes. The parameters are then removed from the address bar.
- Day 4: Notification `AI SaaS - Guia - Dia 4` on Opportunity, Days After `mz_guide_sent_on` + 4, condition on stage Subscribed / Aware, recipient `contact_email`; the monthly-communication chapter applied to the sector, the full offer, the `/registo` link. Any stage change stops it.
- `enroll(lead)` — the Lead button for imported contacts: Opportunity at Subscribed + `send_guide`; a Lead with an Open Opportunity is left alone.

## Layer 3 — auxiliary emails

`content.send_auxiliary(email_template, dry_run)` (Sales Manager): the team writes an Email Template (Jinja sees `doc` = the Lead row, `opportunity`, `greeting`, `signature`, `unsubscribe_url`) and runs the verb from `bench execute` or the desk. Recipients: Open Opportunities at Subscribed / Aware whose Lead is not unsubscribed. Each send increments `mz_aux_sent`; reaching the cap reports Dormant. Dry run returns who would receive it. Not Frappe Newsletter: it cannot read the stage, cannot personalise per company, and would need a mirror of the Lead list in an Email Group.

## Suppression

`/sair?l=<lead>&t=<hmac>` is a page with a button (mail scanners prefetch links; a prefetch must never unsubscribe anyone) → `api/content.unsubscribe` → `content.suppress`: `Lead.unsubscribed = 1`, `mz_fiscal_alerts = 0`, status Do Not Contact; a content-stage Opportunity is reported Unsubscribed / Lost. An account in trial or paying keeps its funnel stage and its transactional mail — only the content stops. Never Frappe's `global_unsubscribe`, which would also silence invoices. Subscribing again by hand at `/alerta-fiscal` is consent: the flag comes back and a fresh Opportunity opens (the Lost one stays).

## Pages and endpoints

- `/alerta-fiscal` (`www/alerta-fiscal/`): the page of the Meta ads — sells the alert, never the software. Lists the enabled obligations, asks name, email, company, sector (the 17 segments), optional VAT regime. Meta Pixel + `Lead` event when `meta_pixel_id` is set. Same posture as `/registo`: shared stylesheet, `fetch` not `frappe.call`, inline errors.
- `api/content.py`: `subscribe` (POST, `_limit` 10/10 min per IP + 5/h per email), `guide_opened` (60/min), `unsubscribe` (POST, 10/min). Nothing reveals whether an address already exists; a second subscription answers exactly like the first.

## Ops on deploy

`bench migrate` as `erp-user` (DocTypes `MZ Fiscal Obligation`, `MZ Guide Segment`; custom fields on Lead, Opportunity, Web Page; the four stages; settings fields; Lead Sources; seed obligations; Lead client script; the notification). Then `bench execute ai_saas.install.push_email_templates` if the guide template's copy is revised later (creation is automatic). No `bench build` needed: `public/js` is served through the existing assets symlink. Then the business work: an accountant fills and reviews the obligations (deadline rules, texts, `reviewed_by` / `reviewed_on`) and enables them; the first guide Web Page is published with its sectors ticked.

## Verified by

`bench --site erp.local run-tests --app ai_saas --module ai_saas.tests.test_fiscal_alerts` and `…test_content_layers`: business days over a weekend and a holiday; the deadline rules incl. clamping and quarterly months; the send window, recipient filters (regime, segment, unsubscribed, unflagged), idempotence per deadline, dry run, the review gate on enabling and on sending; subscription creates Lead + Opportunity at Subscribed with the guide and its per-company link, a second subscription is a quiet no-op, no published guide → no email and no anchor, a Lead already in the funnel is only flagged; a bad token does nothing, opening moves Subscribed → Aware and Dormant → Aware and leaves a trial alone; `enter_crm` advances the same Opportunity to Form Started and resends Dia 0; the auxiliary cap → Dormant, reset on a signal, Dormant excluded; `/sair` flags the Lead and closes the content Opportunity, leaves a trial stage alone, re-subscription reopens; the Day-4 notification's shape, condition and render, and that Form Started stops it.

## Not built

- Double opt-in (the Day-0 guide is the confirmation), reply detection, IMAP, pricing-page tracking on the landing.
- The draft's Interessado and Reciclagem states (see above).
- Frappe Newsletter / Email Group / ERPNext Email Campaign.
- A view-log DocType or `Website Settings.enable_view_tracking`: opens are stamped on the Opportunity, which is what "guide pages opened by sector" is grouped on (Opportunity list, group by the Lead's segment).
- Any change to the Next.js landing page; it links to `/alerta-fiscal` when the owner chooses.
