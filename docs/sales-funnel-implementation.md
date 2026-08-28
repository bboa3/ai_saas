# ai_saas — Sales Funnel Implementation Plan

---

## Context

`sales-funnel.md` in this folder is the business plan: why the funnel is being replaced, the eight problems, and the settled commercial decisions. This document is its implementation counterpart, in the format of `../../erpnext_mz/docs/improvements.md`: every item has a stable ID, a **Today** verified against the code or the live database, a **Change**, a **Risk**, and a way to confirm it is done. The items are catalogued in the order of the funnel itself — signup → contract → provisioning → trial → activation → suspension/archive → recovery — and sequenced for building in the **Execution order** table, which follows dependency, not catalogue order.

Everything in the Today paragraphs was confirmed on this bench: frappe 15.103.3, erpnext 15.103.1, control site `erp.local`, app `apps/ai_saas`. Two decisions from the business plan govern everything here and are not re-litigated: **nothing counts days — the system reads two dates** (`Contract.start_date` ends the trial and starts billing; billing actually begins on the later of that date and the signature date), and **the trial does not use ERPNext's native Subscription trial mechanism** (confirmed sound: `subscription.py:585-594` generates the first invoice only on an exact date match, and `Trialling` status at `subscription.py:224-225` would hide the account from the pre-billing warnings).

**State of the tree.** `provisioning.py` carries uncommitted changes, and `tests/` and `docs/` are untracked. Line numbers below refer to the working tree on 2026-08-24; commit before starting so they stay meaningful.

### Working rules

- **The manual path keeps working through every item.** A contract created signed and submitted by the team must produce exactly today's result, at every point in the sequence.
- **Every item is independently deployable and independently verifiable.**
- **Nothing is written inside the customer's site to run the funnel.** The probe (D1) is read-only; trial communication is email and SMS from the control site.
- **The deactivation engine ships unarmed** (`auto_suspend` / `auto_archive` off) and is armed only at the Milestone, after its daily digest has been read for several days — suspension first, archive later.
- **`erp.local` and `saas.erp.mozeconomia.co.mz` are never test targets.**
- Anything found broken but out of scope goes to *Found, recorded, not being changed* at the end instead of silently into an item.

---

# Part A — Signup

The single, resumable signup form and the guest API behind it. Everything the customer does before an account exists.

**One decision here supersedes the business plan.** `sales-funnel.md` §4 assumed "the form lives on the external landing page and calls guest endpoints in this app". Decided 2026-08-24: **the form is a `www` page on the control site** (A7), following the two proven patterns on this bench — `erpnext/www/book_appointment/` (directory page: `index.py` context, `index.html` extending `templates/web.html`, vanilla JS calling guest-whitelisted endpoints) and `erpnext_mz/www/qr_validation` (`no_cache = 1`, token validated server-side in `get_context` **before** anything renders). The external landing page simply links to it, optionally carrying `?plan=`. The API stays guest-whitelisted, so a future embedded form remains possible without rework.

### A1 — There is no signup API

**Today.** The app has exactly four guest-whitelisted endpoints, all Multipay (`multipay/api.py:31,62,167,201`), each validating an HMAC token before any database access via `erpnext_mz.qr_code.qr_generator.validate_document_hash` (`multipay/api.py:19-23`). There is no endpoint through which an account could be opened. The `www/` directory contains only a stale `pay.cpython-312.pyc` whose source has been deleted.

**Change.** New `ai_saas/api/signup.py` with guest endpoints shaped by A7's steps: `start` (step 1 data → creates **or reuses** the record per A2's one-live-signup rule, returns the resume token, sends the resume email), `update` (saves a step), `check_subdomain` (live availability against A4's slug rules), `submit` (final validation, creates the documents — A4), `status` (provisioning progress for the page to poll; it reconciles the record against the linked `MZ Tenant Provisioning` — `Active` → `Complete`, `Failed` → `Failed` — so the progress view always reaches a terminal state). Every endpoint is `@frappe.whitelist(allow_guest=True)` and rate-limited by a small counter in the site cache (`api/signup._limit`) keyed on the client IP, and — on the token endpoints — additionally on the token *argument*. (Frappe's own `rate_limit` decorator keys on the raw request dict, where `frappe.call`'s arguments are not reliably found; it answered "Either key or IP flag is required" in production and was replaced on 2026-08-25.) Access to an existing record is only by its token; the token is a `frappe.generate_hash()` value stored on the record, never a guessable name.

**Risk.** A public write endpoint. Contained by: rate limits, token-gated access, the duplicate rule (A3), and the ceilings at the Milestone. The Multipay token pattern is proven on this site. `check_subdomain` answers only taken/free, never who holds it.

**Verified by.** From `curl`, with no session: start a signup, update it with the token, be refused without it, and be throttled after the limit.

### A2 — Nothing records a signup in progress

**Today.** Frappe Web Forms cannot resume as Guest — `web_form.py:160-167` throws `PermissionError` the moment a Guest requests a named document, before `allow_edit` is even consulted, and `has_web_form_permission` returns False for Guest unconditionally (`web_form.py:507-509`). So the old plan's "interruptible and resumable" behaviour cannot be a Web Form, and today nothing stores a half-finished signup: an abandoned form is simply gone.

**Change.** New DocType `MZ Signup`: the form's fields (contact, company, NUIT, VAT regime, subdomain, plan), `resume_token` (Data, hidden, unique), `current_step` (Int), per-step timestamps (the funnel metric A7 promises), `status` Select `Started / Superseded / Submitted / Provisioning / Complete / Failed / Duplicate` — writers: `start` sets `Started` (and `Superseded` on any older live row for the same email), `submit` sets `Submitted` then `Provisioning` (A4), `status` reconciles `Complete`/`Failed` from the provisioning record (A1), A3 sets `Duplicate`. There is deliberately no `Abandoned` state: an unfinished signup simply stays `Started`, and G1's Notifications read days-since-activity off it — and links filled at submission (`lead`, `customer`, `contract`). No Guest role in its permissions — all guest access goes through A1's token check, the same posture as `MZ Customer Feedback`. Resume works on two levels, both consequences of A7's step order (email is captured in step 1): the browser keeps the token locally so a same-device return restores silently, and `start` emails a resume link (`/registo?token=...`) so the visitor can continue **on another device** — the page's `get_context` validates the token and re-enters at `current_step`, exactly the `qr_validation` posture.

**One live signup per email.** A lead who ignores the resume email and simply starts again from the landing page must not fork, and must never be sent away to their inbox: **the form is finished in the browser it was started in; the emailed link is only a way back in tomorrow or from another device** (decision 2026-08-25). So `start` always returns a token — but always for a **new** record, never an existing one, because resuming echoes the stored fields back and anyone who knows an email could otherwise read that company's NUIT. The older `Started` row for that address moves to `Superseded`: kept for the funnel history, out of G1's nurture (its condition reads `Started`), so exactly one live signup per address and one resume link that matters. Superseding never strands anyone — the tab that still holds the older token is by definition its owner, and its next save revives it to `Started` (retiring whatever superseded it). Correcting the email in step 1 to an address that already has a live signup behaves the same way, for the same reason. A `Complete` match behaves like a fresh signup in the browser but the email sent is "you already have an account" instead of a resume link — the truth goes to the inbox, never to the unauthenticated browser; the browser is refused only at `submit`, by A3.

**Risk.** Low — a new table nothing else depends on. G1's nurture emails and `start`'s re-send carry the same token, so every link a lead holds stays valid. The resume email is transactional and single-recipient; it doubles as the email-deliverability check the funnel wants anyway.

**Verified by.** Fill step 1, close the browser, open the emailed link on another computer and find the fields present at the right step. Then go back to the landing page and start again with the same email: no second `MZ Signup` exists, the step-1 changes landed on the same record, step-2 fields show empty in the new session but the previously typed values are still on the record — and reappear through the emailed link.

### A3 — Duplicates are neither detected nor reported

**Today.** Nothing checks whether an email or NUIT already exists anywhere in the funnel; the old form would happily insert a second `Lead Onboarding` (its dedup at `lead_onboarding.py:53-59` only guards the ToDo).

**Change.** At `submit` (not per keystroke — no enumeration oracle), and keyed on **the company, which is its NUIT** (revised 2026-08-25): the signup is refused when that NUIT already has a cloud account (a submitted Contract with a tenant) or another signup with that NUIT is already past `submit` — one generic failure message that reveals nothing about the existing account, the record marked `Duplicate`, the sales team alerted with the match. An **email** that already belongs to a Customer is not a duplicate: this control site is MozEconomia's own ERP and holds every customer the business has ever had (on-prem, consulting, POS), and one person can represent more than one company — so a known email with a new NUIT goes through, sales gets an informational alert, and A4 reuses the existing Customer rather than opening a second one.

**Risk.** The mirror image of the old risk: two records for one company can only come from two different NUITs (a typo), which the team sees in the trial list. Refusing on email — the first cut of this item — was the worse trade: it shut the door on exactly the people most likely to buy.

**Verified by.** Repeat a signup with the NUIT of a company that already has a site: generic error on the form page, `Duplicate` on the record, alert in the team's inbox, no new Contract. The same email with a different NUIT completes, and its Customer is the existing one.

### A4 — Submission creates nothing by itself

**Today.** The current form's handler `on_lead_onboarding_save` (`lead_onboarding.py:6-48`) copies four fields onto an existing Lead (`:25-32`), flips the Opportunity's `sales_stage` to `Cloud - Form Submitted` (`:46`), and creates a ToDo whose text instructs a human to "Criar o Cliente no ERPNext / Criar e assinar o Contrato" (`:66-70`). It creates no Customer, no Contract, sends no email, and throws if the Lead (`:12-13`) or an Opportunity (`:42-43`) is missing — it cannot serve a visitor who arrives without a nurture link.

**Change.** `submit` stamps the record `Submitted` and creates, with no human involved: the Lead (matched by email, created if none), **the Opportunity** (from the Lead, stage `Cloud - Account Created` — already seeded by `install.py:276-289`; this is the record D2's signals advance and F3/F6 mark Lost, and without it the self-service path would give the CRM nothing to track), the Customer (marked as a trial customer — see below), the primary Contact and the **billing Address** parsed from step 2's one-line address by `mz_address.parse_mz_address` (`address_type = Billing`, street line / bairro / city / province — what `_collect_customer_profile` reads Billing-first into the tenant's company profile — and provisioning runs the same parser (`_structured_address`) over any one-line address it finds, Address line or signup text, so the tenant gets bairro/city/province on the manual path too; E1 offers corrections), the Lead's industry from step 2 (the field G2's segmentation reads), and the Contract — `party_type="Customer"`, plan, subdomain, `contact_email`, `start_date` = creation date + trial length from F1's settings, `end_date` empty (a signed contract with no end date is always Active, `contract.py:116-117`; with one set and `start_date` in the future the native status would go `Inactive`), `contract_terms` rendered server-side from the B4 template — and **submits it**, which triggers provisioning through B1. The Signup record moves to `Provisioning` and the `/registo` page follows via `status`.

**Trial customers must not distort sales reporting.** The Customer is created in a dedicated Customer Group `Cloud - Trial` (created idempotently in `install.py`) and moved to the commercial group at activation (E1). The group is the reporting filter; the contract's phase field (B2) remains the operational truth.

**Risk.** This is the item that turns a web request into database documents. It runs entirely server-side with `ignore_permissions` on documents the API itself composed — no client-supplied doctype or fieldname is ever trusted. The Contract path it exercises is the same one the team uses by hand.

**Verified by.** Walk the whole flow from `curl` before the page exists (Verification 4): one call sequence ends with Lead + trial-group Customer + submitted unsigned Contract + provisioning record, and an email with the account address.

### A5 — The second form is retired

**Today.** `lead-onboarding-form` (`fixtures/web_form.json:37`, `login_required: 0`) feeds the `Lead Onboarding` DocType (which requires a **company logo** to submit) and the handler above. All seven nurture emails hardcode `https://erp.mozeconomia.co.mz/lead-onboarding-form/new?lead=…` in their bodies. The live table has 0 rows — nothing is lost by removing it.

**Change.** In the same deployment as A4: delete the Web Form fixture and the DocType, remove the `Lead Onboarding` doc_events (`hooks.py:21-24`), remove `lead-onboarding-form` from the Web Form fixture filter (`hooks.py:51` — `cloud-feedback` shares that line and stays), delete `saas/lead_onboarding.py`, and let D3 replace the notifications that pointed at the old URL. The `AI SaaS - Lead Form Submetido` notification (Value Change on `sales_stage`) is superseded by A4's own team alert.

**Risk.** None on data (empty table). The only consumers of the form are the nurture bodies D3 deletes anyway; sequence A5 with D3 so no live email ever links to a dead route.

**Verified by.** `/lead-onboarding-form` returns 404; grep of fixtures and hooks for `lead-onboarding` and `Lead Onboarding` returns nothing.

### A6 — A failed provisioning leaves the promise hanging

**Today.** When provisioning exhausts its attempts the customer gets an apology email (`provisioning.py:605-618`) and the alert goes to the Administrator account's email, falling back to a hardcoded `contacto@mozeconomia.co.mz` (`provisioning.py:730-747`). Nothing links the failure back to a signup, because signups do not exist.

**Change.** The Signup record moves to `Failed`, the `/registo` progress view says the account is taking longer than expected, and the team alert (recipient from F1's settings — C3) names the Signup and the Contract. Because Customer and Contract already exist, recovery is C1's retry, not a new signup.

**Risk.** Low; messaging and linkage only.

**Verified by.** Force a failure (bad subdomain collision on a scratch site): the Signup shows `Failed`, the status endpoint reports it, and the alert names the case.

### A7 — The form page: three steps, built for conversion

**Today.** The only public signup form is the retired-in-A5 Web Form: eleven fields on one screen, a **required company logo upload**, placeholder-style layout, no progress indication, no resume, and a success message that promises a human call. Every property of it is on the wrong side of the published evidence on form conversion: multi-step forms convert on the order of 86% better than single-page equivalents (some studies far higher), each extra field costs roughly 4% conversion, and 27% of users abandon outright when a form looks long.

**Change.** New directory page `ai_saas/www/registo/` (`index.py`, `index.html`, `index.js`, `index.css`) in the `book_appointment` structure, with `qr_validation`'s server discipline: `no_cache = 1`, and when `?token=` is present `get_context` validates it against `MZ Signup` before rendering, re-entering at the stored step. All data calls go to A1's endpoints. **Three steps, ordered easy → invested, sensitive last**, so that even a step-1-only visit yields a workable lead:

| Step | Asks | Why here |
|---|---|---|
| **1 · Contact** — "Crie a sua conta" | Full name · work email · WhatsApp/phone. No plan here (decision 2026-08-24: the price is a step-3 decision, not a step-1 distraction). | The highest-value fields first: partial completion is already a usable lead and unlocks resume-by-email (A2) and the unfinished-signup nurture (G1). Three inputs, nothing that requires looking anything up. |
| **2 · Company** — "Sobre a sua empresa" | Company name · NUIT · VAT regime (the existing 4-option select) · industry (Link options from `Segment Intelligence Map`, as the old form had) · **one address line** ("Av. 25 de Setembro, 1234, Bairro Central, Maputo"). | The "go find the NUIT" step — the interruption the resume link exists for. Company name arrives pre-typed as a suggestion where inferable. Industry stays **because remarketing and automation segment on it** (G2's campaigns and D2's signals read it); it renders as a short select, not a search box. One free-text address, written the way Mozambicans say it, is easier than a city box plus lines later; `saas/mz_address.parse_mz_address` splits it into street line, bairro, city and province (decision 2026-08-25), so the **Billing Address** exists with real lines from day one and E1 only offers corrections. `Address.city` is mandatory, and one line does not always name a city ("Av. 25 de Setembro"), so the parser reads a city written any way a person writes it — with or without commas, accents or the administrative wrapper ("Cidade de Maputo", "cidade da Beira", "xai xai", "Rua da Paz nr 4 Vilanculos") — and when it still finds none the step **asks one extra question** (a Cidade box with the known towns as suggestions) instead of guessing a street into the city field or failing at submit. Asked once: coming back to step 2 without changing the address keeps the answer. No logo, no multi-line form. |
| **3 · Plan + account** — "Plano e endereço" | **Plan choice** (radio cards; `?plan=` from the pricing page preselects) · subdomain (auto-suggested from company name, live-checked via `check_subdomain`, shown as `___.erp.mozeconomia.co.mz`) · trial-end date stated · terms acceptance (`/termos`) → **"Criar a minha conta"**. | The commitment step, reached invested. Submission flips the page into the provisioning progress view polling `status`, ending with "verifique o seu email". |

UI rules, applied throughout and treated as acceptance criteria, not taste: single column; labels always above the field, never placeholder-as-label; inline validation on leaving a field (never mid-typing), with error text that says how to fix it and a success mark when it passes; one primary button per step, sentence case, no all-caps; a "Passo N de 3" progress indicator; step 1 fits a phone screen without scrolling; going back never loses data. Trust furniture on every step: "sem cartão de crédito", the trial length, and a one-line data-privacy note. Visual design follows the MozEconomia branding already applied by `erpnext_mz`'s setup (logo, colors) rather than inventing a palette; the website navbar and footer are hidden on this page, as on `qr_validation` — they have nothing to do with the lead. (A fuller brand-card variant was built and reverted on 2026-08-24 at the user's request.)

**Risk.** The page lives on the control site, so it must render nothing about existing documents — it reads and writes only `MZ Signup` through the token. Frappe's `templates/web.html` brings the website theme; if its chrome fights the form, the page goes self-contained the way `qr_validation.html` did. Step design is measurable after launch (each step transition is a timestamp on `MZ Signup`), so the step split can be argued with data later instead of re-litigated now.

**Verified by.** On a phone: arrive from a pricing link, see step 1 complete without scrolling, leave after step 1, receive the resume email, finish on a desktop, watch the progress view end in the welcome email. Field-level errors appear on blur with specific Portuguese messages; the browser back button and the form's own back control both preserve entered data.

*Design references:* [multi-step vs single-step and field-count evidence](https://www.reform.app/blog/multi-step-form-drop-off-rates-how-to-reduce-them) · [which fields belong on step 1](https://tryformbot.com/blog/lead-generation-best-practices) · [SaaS signup patterns — collect essentials now, profile later](https://www.saasui.design/blog/saas-signup-registration-ux-patterns) · [signup vs onboarding split](https://razegrowth.com/blog/saas-sign-up-flow-ux-patterns) · [form UI rules: single column, labels above, inline validation](https://www.designstudiouiux.com/blog/form-ux-design-best-practices/) · [inline validation timing](https://subux.pro/guides/article/inline-validation)

---

# Part B — Contract

The submitted-but-unsigned contract: the document that carries the whole funnel.

### B1 — One trigger does two jobs, gated on the signature

**Today.** `hooks.py:16-20` routes Contract `on_submit` **and** `on_update_after_submit` to `contract_lifecycle.on_contract_signed`, which returns unless `party_type == "Customer"` (`contract_lifecycle.py:6`), **unless `is_signed`** (`:8`), and unless `mz_subscription_plan` is set (`:10`); only then does it create the Subscription (`:13`) and queue provisioning (`:14`). There is no path to a provisioned site without a billing subscription. ERPNext already supports signing after submission: `is_signed`, `signed_on`, `signee` are `allow_on_submit` (`contract.json:54-58,127-139`), and `before_update_after_submit` recomputes `status` Unsigned → Active (`contract.py:64-66,72-76`).

**Change.** Two handlers, split along the signature:

- `on_contract_submitted` (`on_submit`): validates, stamps the phase field (B2) `Trial` (or `Active` if submitted already signed), queues provisioning — **no `is_signed` gate**, while the `mz_subscription_plan` and `mz_tenant` gates stay exactly as today (`contract_lifecycle.py:10,19`): no plan or no subdomain still means no provisioning — and creates the Subscription only when `is_signed` is already 1 (the manual path, unchanged in outcome).
- `on_contract_signed` (`on_update_after_submit`): fires only on the `is_signed` 0→1 transition (compared against `doc.get_doc_before_save()`), creates the Subscription with E2's billing-start rule, stamps phase `Active`. Guarded by `mz_linked_subscription` exactly as today (`:12`), so it can never create a second subscription — nor a second site, because the provisioning record already exists (C1).

**Risk.** The one change that touches the live sales path. The regression check is explicit: a contract created signed and submitted the old way must produce exactly today's result (Verification 5), and the existing dangling-subscription contracts (see *Found, recorded*) show what sloppy state here looks like.

**Verified by.** Submit an unsigned contract with plan + subdomain: site created, **no** Subscription, phase `Trial`, native status `Unsigned`. Flip `is_signed` on the submitted document: Subscription appears, phase `Active`, no second provisioning record.

### B2 — Nothing marks a contract as a trial

**Today.** The only signal is the native `status = "Unsigned"`, which any commercial contract awaiting a signature also carries — so no code may ever be allowed to switch accounts off based on it. The seven custom fields on Contract (`fixtures/custom_field.json`, mirrored in `install.py:87-158`) contain no phase or trial marker.

**Change.** One new custom field in the *MozEconomia Cloud* tab: `mz_account_phase`, Select `"" / Trial / Active / Suspended / Closed`, read-only, `allow_on_submit: 1`, written only by code (B1, E1, F2). **This field is the only thing the F-part engine reads to decide what it may touch.** A contract with an empty phase — every commercial contract created by hand until the team opts in — is invisible to the engine.

**Risk.** None by itself; it is inert until F3 reads it. The empty-by-default rule is what makes it safe.

**Verified by.** The field exists on submitted contracts, cannot be edited in the form, and every existing contract shows it empty after migrate.

### B3 — The plan cannot be changed at signing

**Today.** All seven AI SaaS custom fields are frozen after submit: `fixtures/custom_field.json` carries `allow_on_submit: 0` explicitly, and the `install.py:87-158` definitions omit the key (same effect by default). A submitted contract therefore freezes the plan — but the plan is the one field a customer may legitimately correct at the moment of signing (`sales-funnel.md`, Phase 1). (`mz_linked_subscription` is only written past submit because `frappe.db.set_value` at `contract_lifecycle.py:89` bypasses the check.)

**Change.** `mz_subscription_plan` gets `allow_on_submit: 1`, changed in both places, with a guard in `on_contract_signed`: the plan may change only while no Subscription is linked. NUIT and address need nothing — they live on the Customer and the Address.

**Risk.** Low. The guard closes the one hole (silently swapping the plan under a live subscription).

**Verified by.** Change the plan on a submitted unsigned contract — accepted; on an activated one — refused.

### B4 — Programmatic contracts cannot satisfy `contract_terms`

**Today.** `contract_terms` is `reqd` (`contract.json:172`) and is populated **only** by the desk form's JS (`contract.js:16` calling `get_contract_template`, `contract_template.py:36-47`) — there is no server-side fallback. And **no Contract Template exists on `erp.local`**; `install.py` creates none. A4's API-created contract would fail validation today.

**Change.** `install.py` gains an idempotent `ensure_contract_template()` creating the `MozEconomia Cloud` template (terms text supplied by the business — this is the text the customer accepts at E1). A4 and any other programmatic creation render it server-side with the same `get_contract_template(template_name, doc)` the desk uses.

**Risk.** None; additive.

**Verified by.** A fresh scratch site, after migrate, has the template; A4's flow produces a contract whose terms are the rendered template.

### B5 — Dead `contact_person` code

**Today.** `install.py:46` lists `contact_person` in `_STALE_FIELDS` and deletes the custom field on every migrate — yet `contract_lifecycle.py:101` still reads `doc.get("contact_person")` to set the customer's primary contact. The field never exists, so `_ensure_customer_primary_contact` silently does nothing beyond its guard.

**Change.** Rewire `_ensure_customer_primary_contact` to resolve the contact the way `provisioning.py:754-783` already does (Customer → primary Contact via Dynamic Link), and drop the phantom-field read. A4 creates the Contact at signup, so the chain Customer → `customer_primary_contact` → `email_id` (which every invoice notification depends on) is complete from day one.

**Risk.** Low; the current code is a no-op, so any behaviour is an improvement. Keep the existing "never overwrite" guard (`contract_lifecycle.py:105-107`).

**Verified by.** After a signup, the Customer shows a primary contact and a non-NULL `email_id` without human help.

---

# Part C — Provisioning

Provisioning goes back to being provisioning: it creates a site and reports its state.

### C1 — A failed provisioning is dead forever

**Today.** `provision_tenant` returns silently if **any** `MZ Tenant Provisioning` record exists for the contract (`provisioning.py:59-61`) — including a `Failed` one — and `retry_stuck_provisioning` deliberately skips `Failed` (`provisioning.py:111-120`). So once the three attempts are spent, no re-submission, no signature, nothing revives the request; the only path is manual surgery. The good half of this design is the one the funnel depends on and keeps: one record per contract for life means **signing later can never create a second site**.

**Change.** In `provision_tenant`: if the existing record is `Failed`, reset `attempts`, log the manual retrigger, and re-enqueue; every other status keeps the silent return. Add a "Retry" button on the DocType calling the same path, so A6's alert has a one-click answer.

**Risk.** A retry against a half-created site is already handled — `_step_create_site` skips `bench new-site` when the site directory exists (`provisioning.py:172-175`).

**Verified by.** Fail a provisioning three times on a scratch slug, press Retry, watch it complete against the half-built site. Verification 2 confirms no second record across signature.

### C2 — The delivery email tells the wrong story

**Today.** The welcome email is an 880-line module's inline HTML f-string (`provisioning.py:628-727`), sent via bare `frappe.sendmail` with no template and no configurable sender (`provisioning.py:720-727`). Its copy assumes a paying customer: "As suas faturas mensais chegam a este email com 7 dias de prazo para pagamento" (`provisioning.py:663`) and "A nossa equipa vai contactar nas próximas horas" (`provisioning.py:659`) — both false for a trial that begins at 11pm on a Saturday. (The README also documents an `AI SaaS - Boas-Vindas` Notification that does not exist in the fixtures; the code email is the only welcome.)

**Change.** The email body moves to an **Email Template** rendered with the contract in context, and the copy becomes the trial delivery: address, credentials link, the date the trial ends (`Contract.start_date`), and both paths always present — activate (via E1's `get_activation_url` helper) or book a call (D4's page). Billing copy appears only when the contract is already signed (the manual path). `provisioning.py` keeps only the send. **Because both links are other items' deliverables, C2 ships in execution row 6, after E1 and alongside D3/D4** — until then the current email keeps sending.

**Risk.** None mechanical. Copy is a business deliverable; the item ships with placeholder-free Portuguese reviewed by the team.

**Verified by.** Provision a trial contract: the email names the real trial-end date and contains working activate and booking links; provision a signed one: billing copy is back.

### C3 — Failure alerts go to "Administrator"

**Today.** `_send_failure_alert` resolves its recipient as the Administrator user's email or the hardcoded `contacto@mozeconomia.co.mz` (`provisioning.py:730-747`). There is no configurable operations recipient anywhere in the app.

**Change.** Recipient comes from `MZ SaaS Settings` (F1), one field shared by every internal alert in this plan (A3, A6, F3's dry-run digest).

**Risk.** None.

**Verified by.** Set the field, force a failure, and the alert lands there.
### C4 — Every site gets a scheduler, but the scheduler is a Premium feature

**Today.** Provisioning never touches the tenant's scheduler: `PROVISIONING_STEPS` (`provisioning.py`) creates the site, runs the wizard, seeds the company and notifies — whatever `System Settings.enable_scheduler` ends up as (`frappe.utils.scheduler.is_scheduler_disabled` reads it) is Frappe's default, not a decision. Decided 2026-08-24: background jobs on the customer's site are a **Premium** feature.

**Change.** The decision lives in one table the team owns: `MZ SaaS Settings.scheduler_plans` (Table MultiSelect of Subscription Plans, seeded once with the plans named Premium). A contract's plan in the table → scheduler on; anything else → off. No naming or Item convention to remember. An explicit provisioning step `_step_apply_scheduler_policy` after the wizard and system settings (either could touch the setting) runs `bench --site <site> enable-scheduler` or `disable-scheduler` by the contract's plan, **both ways**, logged on the provisioning record. Because the plan can be corrected at activation (B3/E1), `on_contract_signed` re-applies the policy through `provisioning.apply_scheduler_policy` — a failing bench command never undoes the signature: it is logged and ops are alerted.

**Risk.** A tenant on a non-Premium plan loses scheduled jobs inside its site (email queue flushing, auto-repeat, reports). That is the product decision; the control site's own jobs (probe, lifecycle, dunning) are unaffected — they run on `erp.local`.

**Verified by.** Provision a contract on a non-Premium plan: the site answers `bench --site <site> doctor` with the scheduler disabled; correct the plan to Premium at activation and sign: the log shows `enable-scheduler` at signature and the scheduler runs.

---

# Part D — Trial

Measuring the trial and speaking to it truthfully.

### D1 — Nothing reads what happens on a trial site

**Today.** The control site can already run code inside a tenant — six call sites use `bench --site <site> execute` subprocesses (`provisioning.py:220,269,294,318,452,501`) — but `site_helpers.py` contains a single function (`generate_user_reset_link`, `site_helpers.py:11-38`) and nothing ever reads usage back. Qualification is a phone call.

**Change.** `erpnext_mz.utils.tenant_usage.usage_snapshot()` — read-only, returning JSON: submitted Sales Invoice count and first-invoice date, enabled user count, last login (`tabUser.last_login` max), executed over the same `bench execute` mechanism. New `saas/usage_signals.py` daily job walks contracts with `mz_account_phase == "Trial"`, calls the probe, and stores a row in a new `MZ Tenant Usage Snapshot` DocType (contract, date, the four numbers) on the control site. Nothing is written on the tenant.

**Risk.** A dead site must not stall the sweep: per-site timeout, failures recorded on the snapshot row, sweep continues. With `background_workers = 1` the sweep must stay off the `long` queue (see Milestone).

**Verified by.** Issue an invoice on a scratch trial site; the next day's snapshot row carries invoice count 1 and the date.

### D2 — Hot and cold leads are invisible

**Today.** The only automatic commercial signal in the app is `AI SaaS - Escalação Comercial` — an overdue-invoice alert for customers already billing. Trials (which do not exist yet) would emit nothing.

**Change.** Two rules over D1's snapshots, evaluated in the same daily job: **hot** — first invoice appeared: flip the Opportunity's stage (A4 created it) to `Cloud - Trial Engaged` and create a High ToDo; **cold** — past the midpoint between contract creation and `start_date` with zero logins and zero invoices: stage `Cloud - Trial At Risk` and its ToDo. Both new Sales Stages join the existing seeder (`install.py:276-289`); the ToDo's `allocated_to` is the Opportunity's assignee when one exists, else `default_sales_user` from F1 — the cited pattern (`billing_monitor.py:165-183`) creates unassigned ToDos, which is not enough here. Both rules fire once per contract (dedup on stage or an existing open ToDo).

**Risk.** Low; CRM writes only.

**Verified by.** The Verification 1 lab run produces the hot signal the day after the invoice; a scratch trial left untouched past midpoint produces the cold one.

### D3 — The nurture emails promise what nobody can do

**Today.** Seven `AI SaaS - Lead Nurture - Dia N` notifications on **Opportunity** (Dia 0 on `New`, the rest `Days After creation` at 3/5/10/15/20/30) ask the reader to "activate" an account that does not exist, link the retired form via a **hardcoded host** (`https://erp.mozeconomia.co.mz/lead-onboarding-form/new?...` in every body), send to `contact_email` plus role **All**, and `Dia 10` carries the Calendly link (`notification.json:532`) — the only Calendly reference in the app. Separately, the three `Pós-Contrato Dia 3/5/33` notifications anchor on Contract `start_date` with condition `doc.is_signed`.

**Change.** In one deployment: the seven nurture notifications leave the Opportunity (G1 re-targets them to unfinished signups) and the trial countdown takes their place on **Contract**, all conditioned on `not doc.is_signed` and `doc.mz_account_phase == "Trial"`:

- delivery/first-steps: covered by C2's email at provisioning;
- countdown: `Days Before start_date` at 7, 3, and 1, plus day 0 — copy escalating in explicitness about what happens on the date, each carrying the activate link (E1) and the booking link (D4);
- copy branches on D1's facts where it matters (already-invoiced vs never-logged-in), via Jinja over the latest snapshot.

Changing a trial's `start_date` moves the whole countdown with it, because every anchor is that field. The `role: All` recipient leak and the hardcoded host go with the deletion; new bodies build URLs with `frappe.utils.get_url`.

**Risk.** Notifications with `Days Before` fire on the daily scheduler; a paused scheduler silently skips a day — accepted, same exposure as every dunning email today. No moment exists where old and new sets are both enabled (single fixture deployment).

**Verified by.** Verification 3: shift a trial contract's `start_date` and confirm the pending countdown moves; grep fixtures for `calendly` and `lead-onboarding` returns nothing; no notification recipient is a bare role `All`.

### D4 — Scheduling is a personal Calendly link

**Today.** ERPNext's native scheduling is completely unused: no `Appointment` or `Appointment Booking Settings` reference exists anywhere in the app's code or fixtures. The native machinery is ready: a guest booking page at `/book_appointment` (`erpnext/www/book_appointment/index.py`), slot generation from settings, auto-link to the Lead by email (`appointment.py:37-51`), least-loaded agent assignment (`appointment.py:154-167`), and a confirmation email (`appointment.py:83-102`).

**Change.** `install.py` seeds `Appointment Booking Settings` idempotently — agent list, slot table, duration, holiday list, `enable_scheduling` — with values the team confirms. Every "book a call" link in C2 and D3 points at `/book_appointment` on the control site. One caveat the native code imposes (`appointment.py:72-81`): its two `after_insert` branches are mutually exclusive — a booking whose email matches a Lead gets agent assignment and a calendar Event but **no email to the customer**, while an unknown email gets only the verify-your-email message (`send_confirmation_email`, `appointment.py:83-102`, is that verification, not a booking confirmation). Since our leads always arrive with a known email, D4 adds one small Notification on Appointment insert confirming the booked time to the customer.

**Risk.** The native page's guest endpoints have no rate limiting (`index.py:28,39,46,96`) — recorded at the end, mitigated by the Milestone's per-IP ceiling at the proxy. Native assignment only ever considers the first least-loaded candidate (`appointment.py:164-167`) — cosmetic, accepted.

**Verified by.** Book as a guest with a known Lead email: appointment `Open`, linked to the Lead, agent assigned, calendar Event created, and the new confirmation Notification received. Book with an unknown email: `Unverified` plus the native verification email, and verifying creates the Lead.

---

# Part E — Activation

Signing is activating. The customer converts alone, at any hour.

### E1 — There is no way to sign without the desk

**Today.** `is_signed` can only be flipped inside the desk UI by a user with write permission on Contract. Customers have no account on `erp.local` and never will.

**Change.** New page `ai_saas/www/activar.py|html`, token-gated like the Multipay pages (HMAC over the contract name via the `validate_document_hash` pattern, `multipay/api.py:19-23`). It shows — asks for no new choices — the plan chosen at signup and the billing details on file, allows correcting plan (B3), NUIT, and contact, and **completes the billing address**: signup collected only the city (A7), so this page presents the Billing Address for the customer to fill in the lines the invoice will carry — the one datum legitimately still missing, asked at the moment it becomes real. It displays the contract terms (B4), and on confirmation sets `is_signed = 1`, `signed_on`, `signee` (all natively `allow_on_submit`) — **through `frappe.get_doc(...).save(ignore_permissions=True)`, never `frappe.db.set_value`**: `on_update_after_submit` fires only on the document save path (`frappe/model/document.py:1184-1185`), `get_doc_before_save()` (B1's 0→1 detection) is populated only there, and the app's own `db.set_value` precedent on submitted contracts (`contract_lifecycle.py:89`) is exactly what must *not* be copied here, or the whole chain — Subscription, phase, status recompute — silently never runs. That chain: B1's `on_contract_signed` → Subscription, phase `Active`, Customer moved out of the `Cloud - Trial` group. **If the contract's phase is `Suspended` when the customer signs** (the activation links in old emails outlive a suspension), the confirmation also calls `tenant_lifecycle.reactivate` (F2) before stamping `Active` — billing must never start against a site answering 503. E1 also owns the small helper the messaging items call: `get_activation_url(contract)` (HMAC token + `frappe.utils.get_url`), exposed to Email Template and Notification rendering so C2 and D3 never hand-build the URL. The link appears in C2's delivery email and every D3 message.

**Risk.** The token authorizes exactly one action on exactly one contract; a replay after signature is a no-op (the `mz_linked_subscription` guard). The page runs on the control site — the customer's site is untouched, nothing migrates.

**Superseded in part (2026-08-25).** The page no longer asks for the NUIT, the address or the contract acceptance — see the E1 review entry: signup already collects all three, and every field on this page was a reason to hesitate before the one decision it exists for. `_activate` still accepts those arguments, so the capability is intact for any future caller.

**Verified by.** Verification 2, both before and after `start_date`; repeat the click and confirm nothing doubles.

### E2 — Billing would start on the wrong date

**Today.** `_setup_subscription` copies `start_date=doc.start_date` straight onto the Subscription (`contract_lifecycle.py:75`). Under the new funnel that is wrong in both directions: signing **early** would start billing before the trial ends, and signing **late** would put `current_invoice_start` in the past — where it is unreachable, because invoice generation is a strict equality against one date (`can_generate_new_invoice`, `subscription.py:585-594`), checked once a day (`erpnext/hooks.py:462`), and a past period is rolled forward without invoicing (`subscription.py:565-566`).

**Change.** Billing start = `max(Contract.start_date, signature date)` — the later of the two, exactly the business rule ("signing early never costs money"). The Subscription is created with that as `start_date`, keeping `generate_invoice_at = "Beginning of the current subscription period"` and `days_until_due = 7` (`contract_lifecycle.py:77-79`). When billing start is today, the first invoice is issued immediately by calling the subscription's own `process(posting_date=today)` rather than waiting for the daily job — closing the exact-match trap for the late-signing case.

**Risk.** The strict-equality behaviour is native and versioned; the unit test in Verification 6 pins it so an ERPNext upgrade that changes it fails loudly here, not silently in billing. One deliberate deviation from "the manual path unchanged": a signed contract created with a **past** `start_date` today produces a Subscription whose first period is already unreachable (`subscription.py:565-566` rolls it forward without ever invoicing); under this rule it bills from signature day — an improvement, and the one carve-out Verification 5 records.

**Verified by.** Sign before `start_date`: `current_invoice_start` equals `start_date`, no invoice yet. Sign after: invoice submitted the same day, due in 7.

### E3 — The post-contract emails would anchor on the wrong date

**Today.** The three `AI SaaS - Pós-Contrato Dia 3/5/33` notifications fire `Days After` Contract `start_date` with condition `doc.is_signed` (`notification.json:752,795,838`). Under E2's rule those are different dates: a customer who signs 20 days after the trial ended would get "Já emitiu a sua primeira fatura?" (Dia 3) 17 days late at best, and Dia 33's "Completou o primeiro mês" can predate the first invoice entirely.

**Change.** One new custom field `mz_billing_start` (Date, `allow_on_submit`, read-only), stamped by `on_contract_signed` with E2's computed billing start; the three Pós-Contrato notifications re-anchor their `Days After` on it and add `doc.mz_account_phase == "Active"` to their condition. Empty on every contract signed before this ships — those simply keep firing off `start_date`, which for the manual path is the same date.

**Risk.** None; a field write inside a hook that already runs, plus fixture edits.

**Verified by.** A lab contract signed well after `start_date` gets Dia 3 three days after the signature date, not three days after the trial ended.

---

# Part F — Suspension & Archive

The exit. Built and rehearsed before the front door opens.

### F1 — Every deadline is a hardcoded literal, and there is no off switch

**Today.** `billing_monitor.py` hardcodes its thresholds — pre-billing at D-4 (`:26`), follow-up at ≥7 (`:140`), "deactivation" queue at ≥15 (`:188`) — and has no settings, no dry-run flag, no scoping of any kind. The plan's numbers (suspend at 33 days overdue, then grace, then archive) exist nowhere.

**Change.** New Single `MZ SaaS Settings`: `trial_length_days` (used only at contract creation, A4 — running trials keep their date), `overdue_days_to_suspend` (default 33), `grace_days_to_archive` (default 30), `auto_suspend` and `auto_archive` (two switches — *Suspender automaticamente*, *Arquivar automaticamente* — both default **off**; whatever is not armed is only reported in the daily digest), `ops_alert_recipients` (C3), `default_sales_user` (who D2's ToDos land on), and the Milestone ceilings (max concurrent trials, max signups/day). `billing_monitor` and `tenant_lifecycle` read it; no literal deadline remains in code.

**Risk.** None; it ships with dry-run on and today's numbers as defaults.

**Verified by.** Grep `saas/` for the old literals finds only the settings defaults; flipping a value changes the next run's behaviour.

### F2 — Nothing can suspend, reactivate, or archive a site

**Today.** No code anywhere in the app tears down or blocks a site — grep for suspend/deactivate/drop-site/maintenance across the package hits only `billing_monitor`'s queue-record strings. `MZ Tenant Provisioning` has no lifecycle statuses beyond `Active`/`Failed` (json:68) and no field for a backup location.

**Change.** New `saas/tenant_lifecycle.py`, three idempotent operations, each logging on the provisioning record:

- `suspend(contract)` — `bench --site <site> set-maintenance-mode on` (writes `maintenance_mode` into site config, `frappe/commands/scheduler.py:110-126`): every request answers 503 pre-auth (`frappe/app.py:185-189`) and the tenant's scheduler stops (`frappe/utils/scheduler.py:132-136`); `bench execute` still works, so D1's probe and F2 itself are unaffected. Data untouched. Phase → `Suspended`, provisioning status → `Suspended` (new option), and **`suspended_on` (new Datetime field on the provisioning record) stamped** — the queryable fact the archive rule reads; the free-text log is not it.
- `reactivate(contract)` — the same switch off; phase back to what the contract's state implies (`Trial` if unsigned, `Active` if signed), `suspended_on` cleared. **Reactivating an unsigned trial requires a new `start_date`** — the caller supplies it, and F4's review form makes it mandatory — because a trial whose `start_date` is still in the past would satisfy F3's first rule again and go dark the next morning. One operation, everything back — this is what makes the reactivation offer credible.
- `archive(contract)` — `bench --site <site> backup --with-files`, verify the artifacts exist and are non-empty, then `bench drop-site <site>` (which itself backs up again by default and **moves** the site directory to `<bench>/archived/sites/`, `frappe/commands/site.py:996-1024`). Backup path recorded on the provisioning record (new field `backup_path`); phase → `Closed`, status → `Archived` (new option).

Same subprocess mechanics as provisioning (`_run_cmd`, `provisioning.py:526-552`), same queue discipline.

**Risk.** `archive` is the one irreversible act in this plan. Triple-gated: phase must be `Suspended`, the grace period elapsed, and the backup verified before `drop-site` runs; in dry-run it only logs. `suspend` is fully reversible by construction.

**Verified by.** Verification 1's lab run: login blocked the day after suspension, restored on reactivation, and after archive the site is gone from `sites/`, present under `archived/sites/`, backup on disk at the recorded path.

### F3 — The two switch-off rules do not execute

**Today.** The daily job's endpoint is a row in a queue nobody's code reads: `_process_d15_deactivations` writes an `MZ Overdue Review` with `review_status = "Deactivate"` (`billing_monitor.py:203-214`) and that is the end. The trial-expiry rule cannot exist yet (no trials). The review table is empty on the live site.

**Change.** A daily job in `tenant_lifecycle.py` reading **dates, not day-counts**, and touching only contracts with a non-empty `mz_account_phase` (B2):

- phase `Trial`, unsigned, `start_date` ≤ yesterday → suspend, mark the Opportunity Lost, leave contract and Customer intact (the raw material of G3);
- phase `Active` with an invoice unpaid `overdue_days_to_suspend` past its due date (the existing gate stays: only invoices with `subscription` set, `billing_monitor.py:105`) → suspend;
- phase `Suspended` with `suspended_on` (F2's field) more than `grace_days_to_archive` ago → archive.

While `lifecycle_dry_run` is on, every decision is logged (and digested to C3's recipients) and nothing runs. The engine leaves dry-run only at the Milestone.

**Risk.** The blast-radius question of the whole plan. Contained by: the phase field contract (empty = untouchable), unarmed by default, the suspend/archive gap (nothing is destroyed until a human-visible grace period has passed with the site dark), and the F2 gates.

**Verified by.** Lab: move a trial's `start_date` to yesterday, watch dry-run log exactly that one account, flip dry-run, watch it suspend; a commercial contract with an empty phase and the same dates is never named in the log.

### F4 — The review queue's states are decorative

**Today.** `MZ Overdue Review.review_status` offers `Pending Review / Suspend / Reactivate / Deactivate`, but the controller is `pass` (`mz_overdue_review.py:4-5`); code only ever writes `Pending Review` (`billing_monitor.py:128-135`) and `Deactivate` (`:203-214`), and nothing anywhere reads `Suspend` or `Reactivate`.

**Change.** The controller's `on_update` executes: `Suspend` → `tenant_lifecycle.suspend`, `Reactivate` → `reactivate`, `Deactivate` → suspend now + eligible for archive after grace. This is how the team brings a switch-off forward or reverses it by hand — and it always executes: the `auto_suspend` / `auto_archive` switches arm only the daily job, because a person acting on a specific record *is* the safety check (decision 2026-08-24).

**Verified by.** Set `Suspend` on a lab review: site dark; `Reactivate`: back. All of it in the record's timeline.

### F5 — The warnings do not match what happens

**Today.** `AI SaaS - Aviso de Desativação` fires **8 days** after the due date (`notification.json:352`) telling the customer "A sua conta foi colocada na fila de desactivação" — 7 days before today's code even writes the queue row at D+15, and 25 days before the new engine would actually suspend at D+33. Also: `Lembrete 3` and `Escalação Comercial` both fire at +7 from `posting_date` (README documents different offsets), and the pre-billing email at D-4 promises issuance "em 4 dias" — that one is correct and stays.

**Change.** Dunning realigned to the engine's dates: the deactivation warning moves to shortly before `overdue_days_to_suspend` and its copy states the actual consequence and date; the D+15 review-queue writer in `billing_monitor` is retired in favour of F3; `billing_monitor` keeps pre-billing, reminders, and the D+1 review row, with thresholds from F1.

**Risk.** Notification `days_in_advance` is a fixture constant while `overdue_days_to_suspend` is a setting — the doc records the rule: change one, change both.

**Verified by.** No customer email names an action or date the engine will not actually take (Verification 3's standard); timeline on a lab contract shows warning → suspension in the stated order.

### F6 — Suspension must not orphan the CRM

**Today.** Nothing closes the loop: an expired account would leave an open Opportunity and a Customer indistinguishable from a live one.

**Change.** Folded into F3's transitions: trial expiry marks the Opportunity `Lost` (reason: trial expired — A4 guarantees one exists on the self-service path; when a legacy contract has none, the step skips silently) and leaves the unsigned contract idle; reactivation within grace reopens nothing automatically but G3's segment picks the account up. The exception is E1's sign-while-suspended path, which reactivates the site as part of the signature itself. Customer and data always survive archive — only the site is destroyed, and its backup path is on the record.

**Verified by.** After a lab expiry: Opportunity Lost, Customer intact and still in `Cloud - Trial` group, contract unsigned with phase `Suspended`.

---

# Part G — Recovery

### G1 — Unfinished signups get no nurture

**Today.** Nothing exists (A2 creates the record this segment needs). The seven old `Lead Nurture - Dia N` notifications showed the mechanism that fits here — `Days After` a date field on the document, conditioned on its state — and D3 retires them from the Opportunity.

**Change.** The seven `AI SaaS - Lead Nurture - Dia N` notifications are **adapted, not deleted** (decision 2026-08-24): same names, same seven-touch cadence, re-targeted from Opportunity to `MZ Signup` — `Dia 0` on `New` (it *is* the resume email A2 promised, sent the moment step 1 captures the address), `Dia 3/5/10/15/20/30` as `Days After` `modified` (days since the lead last touched the form), every one conditioned on `doc.status == "Started"` and a captured email, recipient the signup's own `email` (no role `All`), every body carrying the resume link built from `resume_token`. Each stage keeps its original theme — reserved address, social proof, book a call, better than paper/Excel, free setup, last notice — rewritten for someone who has not finished registering. A lead who resumes resets the cadence by touching `modified`; a lead who finishes leaves `Started` and the sequence stops by condition — no state to stamp, nothing to clean up. No job, no threshold setting.

**Risk.** `Days After` fires on the daily scheduler — a skipped day skips that touch, the same exposure as D3 and the dunning set. `modified` is also touched by `status` reconciliation (A1) — but only on records already past `Started`, which the condition excludes.

**Verified by.** A signup left at step 1 gets `Dia 0` immediately and `Dia 3` three days later, each with a working resume link; finishing the form after the first email silences the rest; no nurture recipient is a role.

### G2 — Unconverted leads get no campaign

**Today.** No `Email Campaign` or `Campaign` reference exists in the app. The native machinery runs daily (`email_campaign.py:93-129`, wired at `erpnext/hooks.py:453-454`) off a Campaign's schedule of Email Templates.

**Change.** A `Campaign` with its `Campaign Email Schedule` rows and Email Templates for suspended-unsigned trials; F3's expiry transition enrols the Lead in an `Email Campaign`. Segmentation reads the Lead's industry captured at signup (A7 step 2, `Segment Intelligence Map`) — one campaign per segment where the copy differs, one generic campaign otherwise. Content is a business deliverable.

**Risk.** Native sending is also an exact-date match per schedule row (`email_campaign.py:120-122`) — a missed scheduler day skips that touch. Accepted.

**Verified by.** An expired lab trial appears as an In Progress Email Campaign the next day.

### G3 — Deactivated accounts get no offer

**Today.** Nothing exists; after F2, archived accounts have a recorded backup path and a `Closed` phase.

**Change.** A report/list of `Closed` contracts with backup paths feeding a reactivation campaign (same mechanism as G2) whose offer is concrete: the data is backed up and the account returns as it was — restore being a manual `bench restore` by the team within the backup's lifetime.

**Verified by.** An archived lab account appears in the list with a locatable backup; restoring that backup on a scratch site yields the working site.

---

# Trial usage workflow (D1–D2, as built)

There is exactly one trigger: **the daily scheduler on erp.local**. Nothing on a tenant site ever calls home.

```
Frappe scheduler (erp.local), once a day
 │
 └─ ai_saas.saas.usage_signals.collect_usage_snapshots()
      │
      ├─ SELECT Contract WHERE docstatus=1 AND mz_account_phase='Trial'
      │     (Active / Suspended / Closed accounts are never probed)
      │
      └─ for each trial contract:
           ├─ needs an MZ Tenant Provisioning row with status Active
           │     (not provisioned yet / Failed / Suspended → skipped)
           ├─ already has a snapshot dated today → skipped (idempotent)
           ├─ _probe(site)  →  bench --site <tenant> execute
           │                   erpnext_mz.utils.tenant_usage.usage_snapshot
           │                   (subprocess, read-only, never whitelisted, 60 s timeout)
           ├─ INSERT MZ Tenant Usage Snapshot (the counters, or probe_ok=0 + error)
           └─ if probe_ok: evaluate_signals(contract, snapshot)
                 ├─ score(snapshot) → points + reasons; stored on the row
                 ├─ signal = Engaged | Cooling | Cold | "" ; stored on the row
                 ├─ find the Opportunity (Contract → Customer → Lead → Opportunity)
                 │     none → stop (snapshot kept, no CRM action)
                 └─ Engaged → stage "Cloud - Trial Engaged" + ToDo [Lead quente]
                    Cooling → stage "Cloud - Trial At Risk"  + ToDo [Lead a arrefecer]
                    Cold    → stage "Cloud - Trial At Risk"  + ToDo [Lead frio]
                    (a stage change fires once; each ToDo marker is open at most once;
                     the ToDo's assignee also gets the signal by email)
      │
      └─ send_daily_usage_report()  — only on the full daily sweep, never on a
           contracts=[…] re-run: one email to the sales team listing every trial's
           numbers, score, signal and responsible person
```

A second, independent clock: the four `AI SaaS - Trial - …` countdown Notifications on Contract fire on the calendar (`Days Before start_date`, condition *phase Trial and not signed*) and *read* the latest snapshot to pick their copy. A signal never sends a customer email, never changes the account phase, never suspends — those are `tenant_lifecycle`'s job, driven by `start_date` / `is_signed`.

## What the probe counts

All values are counts, dates or null — never document content.

| Group | Field | Meaning |
|---|---|---|
| lifetime | `invoice_count`, `first_invoice_date` | submitted, non-return Sales Invoices |
| lifetime | `user_count`, `last_login` | enabled System Users / latest login, excluding Administrator and Guest |
| momentum | `invoice_days_7d`, `invoice_days_prev_7d` | distinct days with an invoice, this week vs the week before |
| momentum | `invoice_count_30d` | invoices in the last 30 days |
| breadth | `active_users_7d` | customer users who logged in this week |
| breadth | `master_data_count` | Customer + Item + Supplier rows created by customer users |
| breadth | `other_docs_30d` | submitted Payment Entry, Purchase Invoice, Stock Entry, Salary Slip (where the DocType exists) in 30 days |

## Scoring

| Rule | Points | Why |
|---|---|---|
| `invoice_days_7d ≥ 2` | +2 | invoicing is a habit, not a demo |
| `invoice_days_7d > invoice_days_prev_7d > 0` | +1 | accelerating |
| `active_users_7d ≥ 2` | +1 | more than one person depends on it |
| `master_data_count ≥ 5` | +1 | they moved their own data in |
| `other_docs_30d ≥ 1` | +1 | work beyond invoicing |

- **Engaged** — score ≥ 3. Momentum alone (2) is not enough; one breadth point is required. A single invoice — typically the one created together on the activation call — scores **0**.
- **Cooling** (decay) — an earlier snapshot showed real work (an invoice, or any score), and now: no login for 7+ days and no invoice this week. Having logged in once to look around does not count as "was active".
- **Cold** — past the midpoint between contract creation and `start_date`, no invoice ever and no own master data — whether or not they ever logged in.
- Otherwise no signal; the row still records score and counters for the list view.

Thresholds are constants in `saas/usage_signals.py` (`ENGAGED_THRESHOLD`, `SILENT_DAYS`); the ToDo text lists the reasons that scored so the salesperson sees why.

# Execution order

Dependency, not catalogue. The rule from the business plan holds: **never open the front door before the exit and the till exist.** The manual sales path must keep working after every row.

| # | Items | Why here |
|---|---|---|
| 1 | **B1–B5** trigger split, phase field, plan-on-submit, template, contact fix | Carries the whole funnel; everything below reads the phase field or the split triggers |
| 2 | **F1–F6** settings, lifecycle engine (dry-run), review queue, dunning timing | The exit. Every trial is a site with a database; none may be created unattended before this exists |
| 3 | **C1, C3, C4** failed-retry, alert recipients, scheduler policy | Provisioning stripped of business rules (C2 waits for its links in row 6) |
| 4 | **D1–D2** probe, snapshots, hot/cold signals | Measurement before opening blind; feeds D3's conditional copy |
| 5 | **E1–E3** activation page, billing-start rule, post-contract anchors | The till. Also the links every message in row 6 needs to be true |
| 6 | **C2, D3–D4** delivery email, countdown messaging, native booking | Only now can every email tell the truth; deletes nurture + Calendly |
| 7 | **A1–A7** signup API, record, dedup, creation, form retirement, `/registo` page | The front door, last before the milestone |
| — | **Milestone** (below) | Decision, not development |
| 8 | **G1–G3** remarketing | Needs A2's records, F2's archives, and live traffic to matter (G1 is fixtures only and may ship with row 7) |

# Milestone — open self-service to the public

A decision, with preconditions:

- **Workers.** `common_site_config.json` has `background_workers: 1` and the Procfile runs a single `bench worker` across all queues — one 20-minute `bench new-site` (RQ timeout 1320s, `provisioning.py:95-102`) blocks every scheduled job on the bench. Add a dedicated `long` worker before more than one signup can arrive at once.
- **Ceilings on.** F1's max concurrent trials and signups/day enforced in A1, plus per-IP rate limits at the proxy (covering `/book_appointment`'s unlimited guest endpoints too).
- **The engine out of dry-run** only after its log has named exactly the right accounts, and no others, over several consecutive days.
- **Full rehearsal** with a throwaway subdomain: Verification 1, 2, and 4 end to end.
- **`erpnext_mz` Part B1 (Company creation) deployed** — the prerequisite from `../../erpnext_mz/docs/improvements.md`. Self-service opens only if the instance the customer receives can invoice.

# Verification

> Walk-through with expected results per stage: `sales-funnel-testing.md`.

1. **Block-by-block lab (B, F, C, D):** hand-create a trial contract → site responds → probe returns real numbers → move `start_date` to yesterday → dry-run names it → engine live → login blocked (503) → reactivate → login restored → grace elapsed → archived: site gone, backup at the recorded path.
2. **Activation (E):** repeat the trial and sign once before `start_date`, once after — both attach the Subscription to the same contract, create **no second site**, and bill from the correct date; changing the plan at signing works; a second click on the activation link does nothing.
3. **Messaging (D3, E3, F5):** shift a trial's `start_date` and the countdown moves with it; a late-signed contract's post-contract emails anchor on `mz_billing_start`; no email names an action or a date the system will not actually take; `calendly` and `lead-onboarding` grep clean.
4. **Signup (A):** the whole flow from `curl` before the page exists; then through `/registo`, including interrupt-and-resume on another device and the duplicate-NUIT refusal with team alert.
5. **Regression of the manual path:** after every execution-order row, a contract created signed and submitted the old way produces exactly today's result — with one recorded exception once E2 lands: a backdated signed contract bills from signature day instead of never (E2's Risk).
6. **Automated tests** for the trigger split, billing-start rule (pinning the native strict-date behaviour, `subscription.py:585-594`), slug/dedup validation, and lifecycle transitions in dry-run. Known bench pattern applies: `doc.submit()` commits, so tearDown must clean up explicitly — `frappe.db.rollback()` is not enough.
7. **Never against `erp.local` or `saas.erp.mozeconomia.co.mz`.**

# Review of the implementation

### Row 1 — B1–B5 (2026-08-24)

**Built.** `contract_lifecycle.py` rewritten around the split: `on_contract_submitted` (`on_submit`) stamps the phase and provisions with no `is_signed` gate, creating the Subscription only when submitted already signed; `on_contract_signed` (`on_update_after_submit`) acts solely on the `is_signed` 0→1 transition via `get_doc_before_save()`, with the B3 guard refusing a plan change while `mz_linked_subscription` is set, and keeps the legacy `_maybe_provision_tenant` call as an idempotent safety net. `hooks.py` routes the two events to the two handlers. `install.py` and `fixtures/custom_field.json` gained `mz_account_phase` (Select, read-only, `allow_on_submit`, `no_copy`, after `mz_apps_to_install`) and `allow_on_submit: 1` on `mz_subscription_plan`; `install.py` gained `ensure_contract_template()` (create-if-missing, Jinja terms rendered against the contract), wired into `after_install`/`after_migrate`. `_ensure_customer_primary_contact` resolves through Dynamic Link with primary-contact preference (B5), and `_setup_subscription` now mirrors `mz_linked_subscription` onto the in-memory doc so the same-save B3 guard sees it.

**Deviations from the item text.** None of substance. Two implementation decisions worth recording: the phase is stamped only when both `mz_subscription_plan` **and** `mz_tenant` are set (`_is_cloud_contract`) — a signed contract without a subdomain gets a Subscription but no phase, keeping non-cloud contracts invisible to the engine exactly as B2 requires; and `_setup_subscription` still copies `doc.start_date` verbatim — E2's billing-start rule deliberately waits for row 5, so until then the manual path is bit-for-bit today's behaviour.

**Verified (row 1).** 7 new tests in `tests/test_contract_lifecycle.py` (dispatch both ways, sign-after-submit exactly-once, no-tenant → no phase/no provisioning, B3 allow-then-refuse, B4 template renders placeholder-free, B5 resolution + never-overwrite) — all green, full app suite 14/14, no test residue on `erp.local` afterwards. Applied to `erp.local` via `ai_saas.install.after_migrate`: field flags and the `MozEconomia Cloud` template confirmed in the DB, and all 10 pre-existing contracts show an empty phase. **Running web/worker processes must be restarted** to import the new handlers — until then submits still run the old single-trigger code.

### Row 2 — F1–F6 (2026-08-24)

**Built.** New Single `MZ SaaS Settings` (`ai_saas/doctype/mz_saas_settings/`) with the F1 fields plus two the item list had left implicit — `prebilling_reminder_days` (4) and `overdue_followup_days` (7) — so `billing_monitor` truly holds no literal deadline. Read only through `tenant_lifecycle.get_settings()`, whose fail-safe matters: an unsaved Single returns `None` for every field, and `None` reads as **dry-run ON**. New `saas/tenant_lifecycle.py`: `suspend` (`set-maintenance-mode on`, phase → `Suspended`, `suspended_on` stamped), `reactivate` (refuses an expired unsigned trial without a new `start_date` — the re-suspend-next-morning loop), `archive` (own `backup --with-files`, verified non-empty and fresh, then `drop-site` with root credentials; `backup_path` recorded; refuses unless both provisioning status *and* contract phase are `Suspended`), and `process_lifecycle` — the three daily rules, dry-run logging + ops digest, wired into `scheduler_events.daily`. `MZ Tenant Provisioning` gained `Suspended`/`Archived` statuses, `suspended_on`, `backup_path`; `MZ Overdue Review` gained `new_trial_end_date` and a controller that dispatches Suspend/Reactivate/Deactivate on genuine state transitions only (compared via `get_doc_before_save`), honouring dry-run with an explaining `msgprint`. `billing_monitor`: thresholds from settings, `_process_d15_deactivations` deleted, `_create_d7_followup_tasks` renamed `_create_followup_tasks`. Fixture `AI SaaS - Aviso de Desativação`: `days_in_advance` 8 → **30**, and the copy now states the true consequence — suspension on `due_date + 33` with data intact — instead of the fictitious "fila de desactivação"; synced to the live record.

**Deviations.** `signup_abandoned_after_days` was built and then **removed the same day** (decision 2026-08-24: unfinished-signup nurture is native Notifications on `MZ Signup`, days-since-activity × status — see the rewritten G1 — not a job with a threshold; the DocType was resynced and the field is gone from `erp.local`). Two additions beyond the item text, both recorded above: the two extra settings fields, and `_mark_opportunity_lost` uses a plain `db.set_value` to `Lost` (no lost-reason child rows) — good enough for the engine, revisit if reporting needs reasons. `Aviso de Suspensão` (D+1 after due date, subject claiming imminent suspension) was left untouched: F5's text scoped only the deactivation warning — flagged for the team as a copy decision.

**Verified (row 2).** 5 new tests in `tests/test_tenant_lifecycle.py`: fail-safe defaults; dry-run engine names exactly the phased expired trial and never the phase-less contract with identical dates, executing nothing; suspend/reactivate roundtrip incl. idempotence and the mandatory-new-date refusal; archive refuses an unsuspended site; review queue dispatches on transition and not on a notes edit. Full app suite 19/19; zero residue on `erp.local`. DocTypes synced via `reload-doc`, fixture via `sync_fixtures` — live record confirmed at 30 days/new copy. The engine ships **in dry-run** (and reads dry-run ON even before the Single is first saved). Same restart caveat as row 1 for the new daily job and handlers.

### Row 3 — C1, C3 (2026-08-24)

**Built.** `provision_tenant` now reads the existing record's status: `Failed` is re-queued through the new `retry_failed_provisioning()` (attempts back to 0, `last_error` cleared, log line naming the trigger, fresh timestamped `job_id`); every other status keeps the silent return, so one-record-per-contract-for-life still holds. A whitelisted `retry_provisioning(name)` (write-permission checked) backs the new **"Tentar Novamente"** button in `mz_tenant_provisioning.js`, shown only on `Failed` records, with a confirm dialog. `_send_failure_alert` recipients come from `_ops_alert_recipients()` — `MZ SaaS Settings.ops_alert_recipients` first, the old Administrator-or-`contacto@` fallback only while the setting is empty (imported locally to avoid the `tenant_lifecycle` ↔ `provisioning` import cycle).

**Deviations.** None. C2 (delivery email) deliberately waits for row 6 as the execution order states.

**Verified (row 3).** 4 new tests in `tests/test_provisioning_retry.py` (`frappe.enqueue` patched): a `Failed` record is re-queued in place — status `Queued`, attempts 0, exactly one enqueue, still exactly one record for the contract; `Active` keeps the silent return; retry refuses non-`Failed`; recipients prefer settings and fall back when empty. Full app suite 23/23; zero residue on `erp.local`.

### Row 4 — D1, D2 (2026-08-24)

**Built.** `erpnext_mz.utils.tenant_usage.usage_snapshot()` — the read-only probe, run inside the tenant via `bench --site <site> execute` (it lives in erpnext_mz because that is the app every tenant has installed; `bench execute` refuses a method from an app the site lacks, which is why the ai_saas-hosted version failed on real tenants). It is deliberately never `@frappe.whitelist()`ed — un-whitelisted it is reachable only from a shell on the host, never over HTTP on the tenant; `tests/test_usage_signals.py` asserts this. It returns: submitted non-return Sales Invoice count and first posting date, enabled System Users excluding Administrator/Guest, and `max(last_login)` over the same set (so provisioning's own activity never counts as the customer's). New DocType `MZ Tenant Usage Snapshot` (contract, site, date, the four numbers, `probe_ok`, `error`; read-only for Sales roles). New `saas/usage_signals.py`: `collect_usage_snapshots` daily job over phase-`Trial` contracts whose provisioning record is `Active`, one row per contract per day, a dead site recorded on its row and the sweep continuing (`_run_cmd_capture` given a throwaway log target, 60 s timeout); `evaluate_signals` — **hot** on the first invoice → stage `Cloud - Trial Engaged` + High ToDo, **cold** past the creation→`start_date` midpoint with no login and no invoice → `Cloud - Trial At Risk` + ToDo, each once (dedup on stage and a ToDo marker). New `saas/crm.py`: `find_opportunity(contract)` (Customer party → `Customer.lead_name` party — the shape A4 will produce → `customer_name`), `set_opportunity_stage`, `create_sales_todo` (allocated to the Opportunity's `_assign` else `default_sales_user`). F6's `_mark_opportunity_lost` now uses the same resolver. Both stages added to `install.py`'s seeder; the job registered under `scheduler_events.daily` — the default queue, not `long`.

**Deviations.** None. The probe was executed against `erp.local` itself (read-only) to validate the SQL and the double-JSON-encoded stdout contract before any test was written: 26 invoices, first 2026-08-08, 5 users — correct.

**Verified (row 4).** 6 new tests in `tests/test_usage_signals.py`: probe parsing and failure capture; one snapshot per day with a single probe call; suspended sites skipped; hot fires once with a High marker ToDo; cold fires only past the midpoint; the Opportunity resolver and its Lost exclusion. Full app suite 29/29; zero residue. The end-to-end read against a real tenant belongs to the Milestone rehearsal (Verification 1).

### Row 5 — E1–E3 (2026-08-24)

**Built.** `saas/activation.py`: the token (`erpnext_mz`'s HMAC — `_generate_validation_hash("Contract", name)`, validated with `validate_document_hash`), `get_activation_url()` (exposed to Jinja through a new `hooks.jinja` → `ai_saas/utils/jinja.py`, so C2/D3 render it rather than hand-build), `get_activation_context()` (validates before reading, then plan list, Customer NUIT/phone, billing Address, terms, trial end), and the transaction `_activate()` behind the guest endpoint `activate` (rate-limited per contract): corrects plan (refused once a Subscription is linked — B3), NUIT, phone, upserts the **Billing** Address, then signs **through `doc.save()`** — `is_signed`, `signed_on`, `signee` — so B1's chain runs; if the phase was `Suspended` it calls `tenant_lifecycle.reactivate` after signing, never billing a 503 site; a second click returns `already_signed` and creates nothing. Page `www/activar.py|html` (`no_cache = 1`, `templates/web.html` chrome, Gotham + `erpnext_mz` logo, three states: invalid link / form / already active), vanilla JS calling the endpoint. **E2** in `contract_lifecycle`: `compute_billing_start()` = later of `start_date` and today; the Subscription starts there; when that is today, `sub.process(posting_date=today)` issues the first invoice immediately (failure logged, never blocking the signature). **E3**: custom field `mz_billing_start` (read-only, `allow_on_submit`, in `install.py` + fixture), stamped alongside `mz_linked_subscription`; the three `Pós-Contrato` notifications re-anchored to it; `install.backfill_billing_start()` fills it from the linked Subscription for contracts signed before the field existed (idempotent, in `after_migrate`).

**Deviations.** E3's condition is `doc.mz_billing_start and (not doc.mz_account_phase or doc.mz_account_phase == "Active")` rather than the item's `== "Active"` alone — the 10 pre-existing contracts have an empty phase by B2's rule and would otherwise lose their post-contract emails; the backfill exists for the same reason (it stamped `CON-2026-00009/10`; `CON-2026-00007`'s subscription no longer exists, so it stays empty — see *Found, recorded*). The `activate` wrapper applies `rate_limit` inside the function body (not as a decorator) so `_activate` stays directly testable without a request context.

**Verified (row 5).** 5 new tests in `tests/test_activation.py`: the billing-start rule in all three cases; token gating of the context; bad token → `PermissionError`, unaccepted terms → refused, nothing signed; the full signature — native `status` Active (proof of the save path), phase Active, plan corrected, real Subscription created starting on the future `start_date`, `mz_billing_start` stamped, NUIT and Billing Address written, second click idempotent with exactly one Subscription; sign-while-Suspended calls `reactivate`. Full app suite 34/34; zero residue. Page smoke-tested over HTTP as Guest: bogus token → "Ligação inválida"; valid token on the live unsigned `CON-2026-00008` → form with plans (render only, nothing submitted); valid token on signed `CON-2026-00010` → "já está activa". Fixtures and field synced to `erp.local`; the three notifications confirmed re-anchored in the DB. Late-signing first-invoice generation (`sub.process` today) is covered by logic, not by a test — it needs an invoiceable scratch company and belongs to the Milestone rehearsal (Verification 2).

### Row 6 — C2, D3, D4 (2026-08-24)

**Built.** **C2**: the delivery email is an Email Template, `MozEconomia Cloud - Entrega da Conta` (`install.ensure_email_templates`, create-if-missing), rendered by `provisioning._render_welcome_email` with an explicit context — customer, site, one-time reset link, `is_signed`, trial end, plan, `get_activation_url()`, `/book_appointment` — and sent by `_send_welcome_email`; trial copy carries both paths (activate / book a call) and states billing starts on the trial-end date even if signed today; billing copy ("7 dias") appears only for a signed contract. The 100-line inline f-string and its "a nossa equipa vai contactar" promise are gone. **D3**: the seven `Lead Nurture - Dia N` notifications were first deleted, then — same day, on the user's instruction — **adapted instead** into G1's unfinished-signup nurture (see the G1 entry below); the trial countdown is four new notifications on **Contract**, `Days Before start_date` at 7 / 3 / 1 / 0 (`AI SaaS - Trial - 7 dias`, `- 3 dias`, `- Último dia amanhã`, `- Hoje`), condition `docstatus == 1 and not is_signed and mz_account_phase == "Trial" and contact_email`, recipient `contact_email` only (no role `All`), bodies branching on the latest `MZ Tenant Usage Snapshot` (`frappe.get_all` inside the message — available to Notification *messages*, not to *conditions*), URLs from `get_activation_url` / `frappe.utils.get_url`; the Calendly link and the hardcoded host are gone. **D4**: `install.ensure_appointment_booking` seeds `Appointment Booking Settings` once (Mon–Fri 09:00–17:00, 30 min, 14 days ahead, existing Holiday List or a new one, agents = `default_sales_user` else enabled Sales Managers else Administrator, `enable_scheduling` on) and never touches it again; plus Notification `AI SaaS - Marcação Confirmada` on Appointment `New` to `customer_email` — the confirmation native code does not send on the known-party branch.

**Deviations.** `install.retire_notifications` was written and withdrawn the same day once the nurture set was adapted rather than retired. Email Template `subject` is a 140-char Data field, so the subject is short and static-ish rather than naming the trial-end date. The seeded agent on `erp.local` is a single user (no `default_sales_user` set yet; fill it in MZ SaaS Settings and re-seed by clearing the settings, or edit them directly). `AI SaaS - Lead Form Submetido` stays until A5 retires it with the form (row 7).

**Verified (row 6).** 6 new tests in `tests/test_messaging.py`: trial delivery renders both links, the reset key, the trial-end date, no billing copy and no unrendered placeholders; signed delivery renders billing copy and no activation link; `_send_welcome_email` sends the template output to `contact_email`; the nurture set is re-targeted to `MZ Signup` with the right anchors/conditions/recipient, the four countdowns have the right anchor/condition/recipients, and no notification body contains `calendly` or `lead-onboarding`; the 7-day countdown renders end-to-end with the activation link and its condition flips false once signed; booking settings enabled with agents and slots, confirmation notification wired. Full app suite 40/40; zero residue. **Applied to `erp.local` with the bench stopped** (Redis/web/workers were down — they must be started again for the daily jobs, the web pages and the new hooks to run). Two defects caught during application: the subject length limit above, and my test initially rendering the message with the condition namespace — the fixture itself was right.

### G1 (pulled forward with row 6) — unfinished-signup nurture (2026-08-24)

**Built.** The `MZ Signup` DocType from A2 (`ai_saas/doctype/mz_signup/`) created now, because a Notification cannot target a DocType that does not exist: status `Started / Superseded / Submitted / Provisioning / Complete / Failed / Duplicate`, `current_step`, unique hidden `resume_token` (generated in `before_insert`), per-step timestamps, the three steps' fields (contact / company incl. industry → `Segment Intelligence Map` and city / subdomain + plan + terms), links to Lead, Customer, Contract, provisioning record. No Guest role. The seven `AI SaaS - Lead Nurture - Dia N` notifications rewritten in the fixture as G1 specifies — `Dia 0` on `New`, the rest `Days After modified`, condition `doc.status == "Started" and doc.email`, recipient `email`, resume link `/registo?token=…` in every body, original stage themes kept and rewritten for an unfinished registration. Row 7's API will write into this DocType; nothing else changes.

**Verified.** Two tests in `tests/test_messaging.py`: the seven records target `MZ Signup` with the right cadence/anchor/condition/recipient; `Dia 0` and `Dia 30` render end-to-end against a real `MZ Signup` row with the resume link and no unrendered placeholder, and their condition goes false once the status leaves `Started`.

### Row 7 — A1–A7 (2026-08-24)

**Built.** `api/signup.py`: guest endpoints `start`, `update`, `check_subdomain`, `submit`, `status` — thin whitelisted wrappers applying `rate_limit` (per-IP; per-token with `ip_based=False` on the token endpoints) only when an HTTP request exists, over testable core functions. `_start` implements the one-live-signup rule: any non-`Complete` signup for the lowercased email is reused (step-1 fields updated, same token, `Dia 0` re-sent via `Notification.send`, step-2/3 values never echoed); a `Complete` match creates the record as `Duplicate` (so `Dia 0` stays silent), emails "já tem uma conta" and alerts ops — the browser sees the same response either way. `_update` validates per step (NUIT 9 digits, industry exists, subdomain via `provisioning._validate_slug` + taken-check across Contract/provisioning/submitted signups). `_submit`: A3 duplicate rule against Customers (NUIT/email), submitted Contracts and other submitted signups → generic refusal, `Duplicate`, ops alert; F1 ceilings enforced with the record left `Started`; then `_create_documents` — Lead (matched by email, industry on the new `Lead.mz_segment` custom field), **Opportunity** (from Lead, `Cloud - Account Created`), Customer in `Cloud - Trial` (root group and territory resolved dynamically — both are translated on this site), primary Contact (Customer `email_id` set), Billing Address seeded from the city, and the unsigned Contract from the B4 template — inserted **and submitted**, so B1 provisions; any exception rolls back, marks `Failed`, alerts. `_status` reconciles `Complete`/`Failed` from the provisioning record. `www/registo/` (`index.py|html|js|css`): three steps per A7 (contact → company → account), plan pill from `?plan=`, inline blur validation with fix-it Portuguese messages, live subdomain check with auto-suggestion from the company name, "Passo N de 3", progress view polling `status` every 15 s with the four terminal states, resume from `?token=` (validated in `get_context`) or from `localStorage`. A5: `lead-onboarding-form`, the `Lead Onboarding` DocType (+ table) and `AI SaaS - Lead Form Submetido` removed from fixtures, hooks and the site (`install.retire_legacy_signup`, `for_reload=True` so no queue dependency); `saas/lead_onboarding.py` deleted. A6: the provisioning failure alert names the `MZ Signup`.

**Deviations.** The Billing Address created at signup uses the city as `address_line1` (Address requires a line) — E1's page prefills it for the customer to replace. `install.after_migrate` cannot complete while Redis is down (`_upsert_property_setter` deletes through `delete_doc`, which enqueues) — the row-7 steps were applied by calling `ensure_trial_customer_group` and `retire_legacy_signup` directly; a normal `bench migrate` with the bench up runs them all. Two of my own translation assumptions ("All Customer Groups", "All Territories") were wrong on this pt-MZ site and are now resolved dynamically in code.

**Verified (row 7).** 7 new tests in `tests/test_signup.py`: start creates and reuses per email without echoing step-2 values; update validates, normalises the NUIT and advances; `status` returns a token holder's own values; subdomain checks (too short, reserved, free, taken by a live contract); full submit creates Lead + Opportunity + trial-group Customer with primary contact and `email_id` + Billing Address + submitted unsigned Contract in phase `Trial` with rendered terms, calls provisioning exactly once, and is idempotent; duplicate NUIT → generic refusal, `Duplicate`, alert, no Contract; a ceiling refuses and leaves the record `Started`; `status` reconciles `failed` and `complete` from the provisioning record. Full app suite 48/48; zero residue. `/registo` rendered through `frappe.website.serve.get_response` with the bench down: 200 OK, all three steps, the plan list and the script present, no traceback; the interactive walk-through over HTTP (interrupt, resume on another device, duplicate NUIT) is Verification 4 and waits for the bench restart.

### Review and fix pass — rows 1–7 (2026-08-24)

Four independent review lanes (correctness, security of the guest surfaces, plan-vs-implementation gaps, code quality/tests) plus my own checks, then one fix pass in severity order. What changed:

**Blockers.** `billing_monitor` selected the child table `plans` as a column — the daily dunning job had crashed on its first query every day (pre-existing; caught by the first `billing_monitor` tests). `api/signup.start` handed the *existing* signup's resume token to anyone typing that email — a pre-auth takeover and NUIT disclosure contradicting A2's own rule; **redesigned**: a match returns only `{"state": "check_email"}` (the resume link goes to the owner, with a 10-minute cooldown per record), a `Complete` match creates a fresh-looking record carrying a hidden `duplicate_of_account` flag (nurture excluded by condition) that is refused only at `submit`. `ensure_trial_customer_group` no longer creates a parentless root on a pre-wizard site.

**Unattended failures.** `process_lifecycle` and `collect_usage_snapshots` now isolate every contract (`_attempt`: commit on success, rollback + `FALHOU …` in the digest + Error Log on failure); `suspend` refuses anything but an `Active` provisioning record. `_submit` links Lead/Customer/Contract onto the signup *before* `contract.submit()` (provisioning commits inside it), fails loudly when no provisioning record appears, and is re-runnable from `Failed` only while no Contract exists; `_status` reconciles from `Failed` too (a C1 retry turns the signup `Complete`). `_activate` refuses archived/unprovisioned contracts and survives a `reactivate` failure with an ops alert. `frappe.db.commit()` removed from `_setup_subscription`, `suspend`, `reactivate`, `archive` — callers commit.

**Correctness.** Customer moves from `Cloud - Trial` to `MZ SaaS Settings.commercial_customer_group` (else Selling Settings' default) at signature — both paths. Slug collisions refused in `provision_tenant` (a unique index is impossible: three historical records share `saas`). `mz_tenant_url` removed from the stale-field list (it was deleted and recreated every migrate). Lifecycle command output is persisted on the provisioning record even when a step raises. Role `All` removed from the four notifications still carrying it (Pós-Contrato now to `contact_email`). `Aviso de Suspensão` and `Escalação Comercial` now state the real suspension date (`due_date + 33`). `reactivate` of a billing customer requires the debt settled or the review queue's explicit `force`. A ToDo with no assignee raises an ops alert. Activation validates the NUIT; step-1 email changes are validated and cannot hijack another live signup; the "já tem conta" email and ceiling alerts are rate-limited.

**Plan gaps.** A6 alerts name the Contract; `/termos` page renders the Contract Template generically and `/registo` links it; Verification 6's strict-date pin exists (`Subscription.can_generate_new_invoice`); README rewritten for the funnel; `web_form_feedback.json` and the stale `pay.pyc` deleted; `DEFAULT_APPS` has one source (`provisioning.py`, rendered into the client script and used by signup).

**Structure.** One source of truth per artefact: Custom Fields and the `start_date` Property Setter are fixtures (`custom_field.json`, new `property_setter.json`) — which also removed the `delete_doc` call that made `after_migrate` depend on Redis; the Client Script stays programmatic (it is rendered) and its fixture is gone. New `saas/alerts.py` (`notify_ops`) replaces three senders. Public names for cross-module helpers (`run_cmd`, `validate_slug`, `get_bench_cmd`…; old names aliased in `provisioning`). Self-service plans are the ones flagged `Subscription Plan.mz_cloud_plan` (seeded once), not a name substring. `tests/helpers.py` creates the plans a fresh site lacks instead of skipping — the suite can no longer be silently green with zero tests.

**Not changed, recorded.** Activation tokens remain non-expiring HMACs of the contract name (acceptable: 64-bit, online-only guessing; an issued-at/nonce is a follow-up). Frappe's Jinja is not autoescaped: the shipped Contract Template interpolates only validated fields, and the `/activar` inputs carry sanitised or validated values — any future template that interpolates free text must escape it. Milestone infrastructure items (second `long` worker, proxy rate limit) are not code.

### C4 — scheduler policy (2026-08-24)

**Built.** `MZ SaaS Settings.scheduler_plans` (Table MultiSelect → new child `MZ Scheduler Plan`), seeded once by `install.ensure_scheduler_plans` with the plans named Premium; `provisioning.scheduler_enabled_for_plan/_contract` decide by membership; `_step_apply_scheduler_policy` in `PROVISIONING_STEPS` between company seeding and SMTP; `apply_scheduler_policy(contract)` for live sites; `contract_lifecycle._apply_scheduler_policy` at signature (errors logged + ops alert, signature kept). Two earlier forms were built and replaced the same day on the user's direction — a flag per plan (`mz_scheduler_enabled`, now in `_STALE_FIELDS`) and an Item-code list — the table is the simplest honest source.

**Verified.** 4 tests in `tests/test_scheduler_policy.py` (`run_cmd` patched, settings patched to list the test plan): membership decides (an unlisted basic test plan → off, no plan → off); the provisioning step issues `disable-scheduler` for an unlisted plan and logs it; signing after correcting the plan to a listed one issues exactly `enable-scheduler` and logs the trigger; a failing bench command alerts ops and leaves the contract signed. On `erp.local` the table holds `Premium Mensal` and `Premium Anual`.

### A2 — the inbox is no longer part of the form (2026-08-25)

**Why.** The review pass had fixed the takeover bug by making `start` return `{"state": "check_email"}` on any live match. That closed the disclosure but put the mailbox *inside* the funnel: a visitor whose browser had lost its token (cleared storage, another browser, a second attempt) could not carry on typing — they had to find an email first. The resume link is a helper, not a step.

**Built.** `start` always returns a token, always for a **new** record, so nothing of an existing signup is ever handed to whoever types an email (the disclosure stays closed — that was the real defect, not the continuation). Older `Started` rows for the same address move to `Superseded` via `_supersede_other_live_signups`, keeping one live signup per email and one nurtured record; the tab holding a superseded token revives it on its next `update` (and supersedes whatever replaced it), so a restart in a second browser never strands the first. The step-1 email correction follows the same rule instead of throwing "use a ligação que recebeu". `Superseded` added to the status Select and mapped to `continue` in `STATE_BY_STATUS`, echoing its own fields on resume. The `check_email` screen, its *reenviar* / *usar outro email* actions and `_resend_resume_email` are gone from `registo` (JS + HTML) and the API.

**Verified.** `tests/test_signup.py` — a second `start` returns a *different* token on a record with none of the first's data while the first turns `Superseded` and keeps its company name; the superseded record's own token still reads its fields back and its next `update` returns it to `Started`; a step-1 email change onto a live signup supersedes that row instead of failing. 71/71 green across the app.


### A4 — an address line that names no city (2026-08-25)

**Why.** `Address.address_line1` and `Address.city` are both mandatory, and the one-line address field can legitimately hold neither in a recognisable form. A signup whose address was "Av. 25 de Setembro" — or "Cidade de maputo", which the parser did not recognise — reached `_create_documents` with an empty city and died there: *"Falta valor para Endereço: Cidade/Município"*, the signup marked `Failed` after the Customer and Contact already existed.

**Built.** Three layers, none of which invents data. (1) `mz_address` recognises far more of what people type: case- and accent-folded matching, the administrative wrapper stripped (`Cidade de` / `Município da` / `Distrito de`), an alias table (`xai xai`, `vilanculos`, `Nacala Porto`, …), and a scan for a city named *anywhere* in a line with no commas — so "Av. 25 de Setembro Maputo" and "cidade da Beira" now parse. It also guarantees `address_line1` is never empty while anything was typed (the bairro moves up, else the city), and refuses to read a `Bairro …` part as a city. (2) When no city can be found, `update` step 2 returns `{"state": "need_city", cities: [...]}` — the form reveals a single **Cidade** box with the known towns as `datalist` suggestions, keeps everything typed, and does not advance; a town outside our list is accepted as typed, since the list is a convenience and not a gate. The question is asked once: returning to step 2 with the address unchanged keeps the answer. (3) `_create_documents` no longer assumes: it builds the Address only when it has both mandatory fields and otherwise creates the account without one (E1 collects it before the first invoice) — a missing address must never cost a customer. `provisioning._structured_address` falls back to the signup's answered city, and E1's `_upsert_billing_address` folds the city to its canonical spelling, parses one out of the line when the box is empty, and stamps the province.

**Verified.** `tests/test_mz_address.py` — wrappers, aliases, comma-less lines, "a street alone names no city", "a bairro is never a city", `address_line1` never empty. `tests/test_signup.py` — step 2 asks instead of throwing, keeps `address` and `current_step`, canonicalises the answered city, does not ask twice, re-derives when the address changes; and the Billing Address is created from the answered city with its province. 77/77 green.


### A3 — "already a customer" is not "already has an account" (2026-08-25)

**Why.** Testing the funnel with a real address surfaced it: `_find_duplicate` refused any signup whose email matched `Customer.email_id`, or whose NUIT matched any Customer at all. On this control site — MozEconomia's own ERP, where every customer of every product line lives — that rule refuses the house's own customers. An on-prem or POS customer who came to buy the cloud product would be told *"Não foi possível concluir o registo com estes dados"* and handed to a queue, and the signup would sit at `Duplicate` with all its data and nowhere to go.

**Built.** The rule is now about **cloud accounts, keyed on the NUIT**: refused only when a submitted Contract with a tenant already exists for that NUIT, or another signup with that NUIT is past `submit`. A matching email is no longer a refusal — it triggers an ops alert at submit and, in A4, the **reuse of the existing Customer** (same NUIT) instead of a second record: its customer group, `email_id` and primary contact set by sales are left as they are, only the missing links are filled, and an existing Contact carrying that email is reused rather than duplicated. The "you already have an account" email now adds that registering *another company* is fine, each company having its own account by NUIT.

The refusal screen also grew a **Começar um novo registo** action: a refusal is about the data typed (a company that already has a site, a mistyped NUIT), never a locked browser — without it a stored token kept the page on the refusal for ever.

**Verified.** `tests/test_signup.py` — a NUIT with an existing cloud contract is still refused generically (record `Duplicate`, ops alerted, nothing created); an existing customer of the house is reused (one Customer for that NUIT, commercial group and sales-entered email untouched, primary-contact chain completed); an email with a completed signup registers a second company successfully while sales is alerted; the same NUIT from a different email is still refused. 79/79 green.


### A7 / A2 — the wait on submit, and test mail in the queue (2026-08-25)

**Why.** Two things the first real end-to-end runs showed. Creating an account is two API calls and six documents (Lead, Opportunity, Customer, Contact, Address, Contract — submitted, which enqueues provisioning): a couple of seconds during which the page sat on step 3 with a dead button before anything moved. And every test run left its Notifications in the site's Email Queue — 674 messages to `example.com` had accumulated over two days, waiting for the day someone switches the scheduler on.

**Built.** The submit handler now shows the progress screen **immediately** ("A criar a sua conta… Estamos a preparar tudo") and lets the answer arrive underneath it; a refusal or an error returns to step 3 intact with the reason under the button. `hooks.before_tests` → `tests/helpers.before_tests` mutes mail for the run (muting stops the sending, not the queuing) and purges the previous run's queued messages whose recipients are *all* `@example.com` — real addresses in the queue are never touched.

**Verified.** The resume link was exercised end to end against the running site: a `Started` signup's `/registo?token=…` returns HTTP 200 with the full resume payload embedded (`current_step: 3`, every stored field), so the emailed link drops the visitor back exactly where they stopped. After the purge the site's queue went from 738 unsent to 64, and 79/79 tests stay green.


### Messaging — the welcome email and the credit note (2026-08-25)

**Why.** Three things, all from reading the live notifications rather than the fixture. `Lead Nurture - Dia 0` was written as a "your registration is saved" nudge, when the first email a lead gets should welcome them and sell the product — the resume link is how they get back in, not the reason to write. Its copy also leaned on "sem cartão de crédito", which sells nothing in Mozambique, where cards are not how business software is bought. And the billing set had no credit note: `Fatura Emitida` and the whole dunning chain were conditioned on `doc.subscription` / `outstanding_amount > 0` with nothing excluding `is_return`, so a credit note could be mailed as an invoice (and, before the SAF-T print format existed, with the wrong layout attached).

**Built.** `Dia 0` is now the welcome: *"Bem-vindo à MozEconomia Cloud — o primeiro passo está dado"*, in Mozambican Portuguese and professional in register, naming what the product does for a Mozambican company (certified invoices with NUIT and IVA, stock and treasury, IRPS/INSS payroll, MZN reporting, WhatsApp support from Maputo), the trial length read from `MZ SaaS Settings.trial_length_days` rather than hardcoded, and the resume link framed as *"se ainda não terminou o registo"*. The credit-card line is gone from the welcome, from the delivery Email Template (`install.py`) and from `/activar` — replaced with "sem qualquer pagamento antecipado" / "não é cobrado nada agora". New notification **`AI SaaS - Nota de Crédito`** (Sales Invoice, on Submit, `doc.is_return == 1 and not doc.is_pos`) from the draft supplied by the business, normalised to the house format: the same `format_date` / `format_value` helpers as the invoice email, `|abs` on the (negative) total, the `Nota de Crédito (MZ)` print format attached, the same `contact_email` + billing-contacts CC fan-out, and the house sign-off. Every other invoice email — `Fatura Emitida`, `Lembrete 1/2/3`, `Escalação Comercial`, `Aviso de Suspensão`, `Aviso de Desativação` — now carries `doc.is_return == 0`.

**Legacy notifications removed (2026-08-25).** The control site still carried the predecessor app's 18 `MZ SaaS - …` Notifications, enabled and duplicating the `AI SaaS - …` set one for one — invoice, all three reminders, escalation, suspension, deactivation, post-contract, plus the seven `Lead Nurture` records still firing on **Opportunity** (the source of the 141 queued Opportunity messages). Every invoice would have been mailed twice and every signup nudged twice. Disabled, then deleted at the owner's instruction, with a full dump of both tables kept at `sites/erp.local/private/backups/legacy-mz-saas-notifications-2026-08-25.json`. `mz_saas` is no longer an installed app on this bench, so nothing recreates them; 25 `AI SaaS - …` records remain and no orphan recipient rows.

**Verified.** `tests/test_messaging.py` — the welcome renders with the company name, the trial length from settings and the resume link, and no credit-card line; the credit note is configured (print format, attachment, CC fan-out) and renders both branches (`anula a fatura …` / `crédito a seu favor`) with the amount positive; and every invoice notification's condition evaluates False against a credit note and True against an invoice, with the credit-note condition the exact mirror. 79/79 green, fixtures synced to `erp.local`.


### A4 — the Customer must leave addressable (2026-08-25)

**Why.** A4 created the Contact and the Billing Address and linked both to the Customer, but the Customer itself pointed at neither: `customer_primary_contact` was filled only when blank and `customer_primary_address` never at all, and the Address carried no `is_primary_address`. ERPNext addresses a customer through exactly those two fields — `Customer.email_id` is fetched from the primary contact and `Sales Invoice.contact_email` from that, while print formats and party details read `customer_primary_address`. On the live site both accounts provisioned on 25 August had a primary contact and **no** primary address.

**Built.** New `saas/party.py` with `set_customer_primaries()` (fills only blanks — what sales entered on an existing customer is never overwritten) and `ensure_customer_primaries()` (the safety net that resolves both through Dynamic Link, preferring the flagged-primary record and then a Billing address). A4 now creates the Contact with `is_primary_contact`, sets that flag on a reused one, creates the Address as `is_primary_address` + `is_shipping_address` the way ERPNext's own `make_address` does, and then points the Customer at both, plus `email_id`, `mobile_no` and the `primary_address` display text. `contract_lifecycle._ensure_customer_primary_contact` — the B5 safety net for desk-made contracts — now covers the address too, and E1 marks the address it creates as primary when the trial had none. `install.backfill_customer_primaries` (in `after_migrate`) fills both fields for cloud customers created before this, from what is already linked; it invents nothing.

**Verified.** `tests/test_signup.py` walks a signup and asserts the whole chain: `customer_primary_contact` and `customer_primary_address` set, `email_id`/`mobile_no` filled, `primary_address` rendered, the Contact flagged primary with its email and *primary mobile* (what the billing SMS reads), and the Address `Billing` + primary + shipping with city and province. On `erp.local` the backfill left every funnel customer with both pointers; the one customer it could not complete has no Contact and no Address linked at all, so there was nothing to point at. 83/83 green.


### E1 — /activar rebuilt on the signup's design, then stripped to the decision (2026-08-25)

**Why.** Two passes on the same day. The page had grown its own look — cards, its own type scale, its own button — so the page a customer signs on read like a different product from the page they signed up on; it led with the plan, carried the whole contract text as a section, and closed on a line that said nothing worth saying (*"Não é cobrado nada agora. A sua conta https://… continua exactamente como está."*). Then, with the design fixed, the remaining question was conversion: this page exists for **one** decision, and everything else on it was a reason to hesitate. The billing details were already collected at signup and the terms already accepted there (`MZ Signup.terms_accepted`), so re-asking for both bought nothing and cost clicks.

**Built.** The page links `/registo/index.css` and uses its classes, so the funnel has **one** design and any change lands on both pages; `activar.py` cache-busts that stylesheet by its own mtime. What remains on it: the title, the lead, the plan (the same pill-plus-*alterar* pattern as signup step 3 — a decision already made, alternatives out of the way until asked for), and the button. The `Dados de facturação` block and the `Contrato` card are gone (decision 2026-08-25: billing data is corrected by the team on request), and consent travels with the button in the clickwrap pattern — the note under it reads *"Ao activar, assina o contrato CON-… (termos de serviço)"* and the client sends `accept_terms: 1`, so the server's guard is unchanged. Nothing else was removed from the API: `_activate` still accepts the NUIT/address/phone arguments and skips each one it is not given.

**Copy (three passes, 2026-08-25).** The first attempt sold the trial — deadlines, prices, *não paga nada hoje* — which are the vendor's concerns, not the customer's. The second spoke about what the trial had produced, built from D1's usage probe. The business supplied the final text, and it is better than both: title **`Active e continue de onde parou`**, lead *"O sistema mantém-se como o deixou. Após a confirmação, a sua conta passa a definitiva e continua a trabalhar sem interrupção."* — continuity stated as fact, no argument about price and no numbers to go stale. The usage lookup added for the second pass was removed with it, so the page renders from the contract alone. The billing date and the contract reference stay in the fine print under the button, where a fact belongs.

**Fixes found by the first real activation (2026-08-25).** Editing the fine print out of the template took the error element and the `</div>` closing `#act-form` with it: the click handler died on `err.classList` (nothing was sent) and, had it succeeded, the success panel would have been hidden inside the form it hides. The script now creates its error line when the page has none, so no future edit can make a failed activation silent. Two things were then wrong on the success screen: the heading and lead — an invitation to activate — stayed above a signed account (they now live in `#act-intro`, hidden with the form), and the billing date printed raw ISO; `_activate` returns `billing_start_display` alongside the raw value, so the page writes it the way it writes every other date.

**Verified.** Fetched from the running site with a real token: HTTP 200, the stylesheet linked with its version, both plans rendered with the contract's own pre-selected, and no billing block, checkbox or terms section anywhere in the response. The final copy renders as supplied. The invalid-token and already-signed branches render on the same design. Activation tests unchanged and green.

### D1 — the probe could never run, and where it had to live (2026-08-25)

**Why.** The first end-to-end review of the funnel found the daily probe dead: `usage_signals._probe` ran `bench --site <tenant> execute ai_saas.saas.site_helpers.usage_snapshot`, but `ai_saas` is deliberately not installed on tenant sites and `frappe.get_attr` refuses a method belonging to an app the site does not have (`AppNotInstalledError`, then the eval fallback's `NameError`). Confirmed against a live tenant. Every snapshot would have landed `probe_ok = 0`, so D2's hot/cold signals could never fire, no Opportunity could ever move to *Trial Engaged* / *At Risk*, and D3's countdown emails always took their no-usage branch. The signal logic was never the problem; it was starved.

**The security question first (decision 2026-08-25).** Asked whether reaching into a customer's site opens a door, the answer is that this channel is the most closed of the three options: a local `subprocess.run([...], shell=False)` on the same host, run by the same OS user that already creates, suspends, backs up and drops these sites — no port, no token, no HTTP path, and the site name is constrained by `SLUG_RE` before it is ever an argument. The alternatives are the doors: an HTTP endpoint on the tenant needs a network-reachable route *and* a per-tenant secret to store, rotate and leak; a direct DB connection from the control site needs the tenant's credentials and grants read of everything rather than four counters.

**Built.** The probe now lives in `erpnext_mz/utils/tenant_usage.py` — the app every tenant actually has installed — and `PROBE_METHOD` points at it, so nothing has to smuggle a module in through an `__import__` expression the way the reset-link helper still does (that call is untouched: it works, and rewriting the welcome-email path was not worth the risk today). Two rules are written into the module and enforced by a test: it is **read-only** (four aggregate indicators — invoices submitted, first invoice date, people who can log in, last login — never document content) and it is **never whitelisted**, which is what keeps it off the network; `test_usage_signals.py` fails if `@frappe.whitelist()` is ever added or if `PROBE_METHOD` drifts.

**Also fixed.** The suite was writing snapshots against production contracts: `collect_usage_snapshots()` sweeps every trial on the site, so a patched probe left `MagicMock` rows on real accounts. The test now records when it started and deletes every snapshot written after that point.

**Verified.** The probe run against the live tenant `boa.erp.mozeconomia.co.mz` returns `{"invoice_count": 0, "first_invoice_date": null, "user_count": 1, "last_login": null}`. The full sweep, run for real on the control site, wrote one row for the only contract still in `Trial` (`probe_ok = 1`) and correctly skipped the one activated an hour earlier. 83/83 green.

**Still open.** The contract the customer signs says nothing about these indicators being collected — the terms cover ownership and backups only. The clause should exist before this runs in production.


# Out of scope

Unchanged from the business plan: no data migration between sites (the trial site is the final site); no use of the native Subscription trial mechanism; no provisioning from draft saves (the trigger stays submission); no day-counting anywhere; no writes inside the customer's site for communication; `AI N8N Configuration` stays unread.

# Found, recorded, not being changed

- **The control site's scheduler is off.** `System Settings.enable_scheduler = 0` on `erp.local`, so nothing scheduled has ever run there: no dunning, no `process_lifecycle`, no usage snapshots — and no email flush, which is why the nurture and resume emails sit at `Not Sent` while the provisioning delivery mail (sent inline) arrives. Two consequences before it is switched on: the queue still holds **11 real invoice/dunning messages from 9–18 August** (including one to a customer address) that would all go out at once, and the daily jobs would start acting on live contracts. Purge or expire the stale invoice mail first, then enable.
- **`host_name` on the control site is `http://127.0.0.1:8000`.** Every link the funnel emails — the resume link, the activation link — is built with `frappe.utils.get_url()`, so in production this must be the public URL of the control site or the links will be unusable outside the server.
- **Dangling subscriptions on unsigned contracts.** `CON-2026-00006` and `CON-2026-00008` are `Unsigned` yet carry `mz_linked_subscription` pointing at Subscriptions that no longer exist. Historical test residue on the control site; harmless to the new triggers (the guard checks the field, not the target), but worth clearing by hand.
- **`db_root_user` is still `root`.** `common_site_config.json` sets `"db_root_user": "root"` explicitly — so the d0c914b mitigation (`provisioning.py:819-827`) resolves to the very account whose socket-auth failure it was written to avoid. Creating the dedicated password-authenticated MariaDB account and pointing the config at it is an ops task, not app code.
- **`/book_appointment` guest endpoints have no rate limit** (`erpnext/www/book_appointment/index.py:28,39,46,96`) and `create_appointment` inserts with `ignore_permissions=True`. Native code; covered by the proxy-level limit at the Milestone rather than a fork.
- **Native agent assignment quirk.** `Appointment.auto_assign` only ever considers the first least-loaded candidate (`appointment.py:164-167`). Cosmetic.

### 2026-08-27 — D4 reversed: Calendly stays, `/book_appointment` dropped

**Decision (user).** ERPNext's native Appointment booking is discarded — Calendly is simpler and already produces good meetings. The only thing the funnel needs is one link.

**Built.** `MZ SaaS Settings.booking_url` (Data, section *Alertas Internos*), seeded by `install.ensure_booking_url()` with the existing Calendly URL when empty and owned by the team afterwards. `provisioning.get_booking_url()` feeds the delivery email; the four trial-countdown notifications and the abandoned-signup message read `frappe.db.get_single_value("MZ SaaS Settings", "booking_url")` in their Jinja. Removed: `install.ensure_appointment_booking()`, the `AI SaaS - Marcação Confirmada` notification (fixture and site), and `enable_scheduling` switched off on erp.local. The `/book_appointment` unlimited-endpoint concern in *Found, recorded, not being changed* is moot.

**Verified.** `test_messaging.py`: delivery email and countdown message contain the settings URL and no `/book_appointment`; no `AI SaaS` notification hardcodes `calendly` or `book_appointment`; the confirmation notification no longer exists. Full suite 84/84.

### 2026-08-27 — D2 scoring: momentum, breadth, decay

**Decision (user).** "First invoice" alone was too weak a hot signal — it is often the invoice we create with the customer on the activation call.

**Built.** Probe (`erpnext_mz.utils.tenant_usage`) now returns ten counters (table above); `MZ Tenant Usage Snapshot` gained the six new columns plus `engagement_score` and `signal`. `usage_signals.score()` produces points and human-readable reasons; `evaluate_signals` returns `Engaged` / `Cooling` / `Cold` / `""`, writes score and signal on the row, and acts on the Opportunity as before with a third ToDo marker `[Lead a arrefecer]` for decay. The Jinja in the countdown emails still reads `invoice_count` / `last_login` and is unaffected.

**Verified.** `test_usage_signals.py` (10 tests): one invoice scores 0 and fires nothing; Engaged needs momentum + breadth and fires once; Cooling needs an earlier active snapshot and 7 silent days; Cold unchanged; the sweep stores score and signal on the row. Probe run live on `boa.erp.mozeconomia.co.mz` (all zeros) and `erp.local` (26 invoices, 74 own master-data rows, 2 → 0 invoice days). Full suite 87/87.

### 2026-08-27 — Review of the day's build: three defects fixed

1. **Tests moved a real Opportunity.** The sweep test fed a score-6 payload through `collect_usage_snapshots()`, which walks *every* trial on the site: `CRM-OPP-2026-00003` (a real customer) went to *Trial Engaged* with a fake `[Lead quente]` ToDo. Reverted by hand. Fix: `collect_usage_snapshots(contracts=None)` — tests always pass their own contract; the same argument serves a manual re-run on one account. The creation-time snapshot cleanup stays as a safety net.
2. **Cooling misclassified "looked around once".** An account that logged in on day 1 and did nothing would have become *Cooling* ("esteve activo") after 7 days. Now "was active" requires an earlier snapshot with an invoice or a score > 0, and *Cold* is "no invoice and no own master data past the midpoint", regardless of logins.
3. **Empty booking link.** `get_booking_url()` returned `""` if the setting were blanked; it now falls back to the shipped Calendly URL.

Also: `engagement_score`, `signal`, `invoice_days_7d` shown in the snapshot list view. Suite 87/87; real CRM rows verified untouched after the run.

**Known gaps, not built.** (a) Probing stops at activation — paying customers (phase Active) get no Cooling signal, which is the churn signal that matters most after the sale; extending the sweep to Active contracts with a separate marker is a small change once wanted. (b) Thresholds are code constants, not settings. (c) No dashboard/Number Card over snapshots; the salesperson sees stage + ToDo + the list view. (d) The `Lead Nurture - Dia 3…30` notifications still form a second messaging clock beside the trial countdown. (e) The contract terms say nothing about usage collection.

### 2026-08-28 — Signals reach people: assignee email + daily usage report

**Why.** A ToDo inserted from code notifies nobody (only desk assignment through `assign_to.add` sends mail), so Hot/Cooling/Cold existed only for whoever opened the desk. The team had no view of trial usage at all.

**Built.** `crm._email_assignee`: every signal ToDo is also mailed to its assignee (`[MozEconomia Cloud] [Lead quente] Empresa`, body = the ToDo text + link to the Opportunity; never raises). `usage_signals.send_daily_usage_report()` runs at the end of the unscoped daily sweep (never on a `contracts=[…]` re-run, so tests and one-account reruns stay silent): subject `Trials dd/mm/yyyy: N activos · X quentes · Y a arrefecer · Z frios`, body from `templates/emails/daily_usage_report.html` — one row per trial with days left, the counters, score, signal and responsible person; unreadable sites show their error. Recipients: new `MZ SaaS Settings.usage_report_recipients`, else the *Comercial por Omissão*'s email, else the ops alert recipients. Nothing is sent on a day with no trials.

**Verified.** Real send on 2026-08-28 to the default sales user (`Trials 28/08/2026: 1 activos · 0 quentes · 0 a arrefecer · 0 frios`, status Sent). Tests: the assignee email carries marker, Opportunity link and reference; the scoped sweep never reports while the daily one does; `usage_report_rows` carries signal/score/opportunity/days left; the report goes to the configured recipients with the customer row and the 🔥 label. `notifications_review.csv` rows updated.

### 2026-08-28 — Customer emails for suspension, archive, reactivation

**Why.** The lifecycle engine acted in silence: the only customer-facing words were the Dunning *avisos* sent before the fact. Nothing said "your account was suspended / archived / is back".

**Built.** `saas/lifecycle_mail.py` — `send_lifecycle_email(kind, contract, **extra)` builds one context (customer, contact, site, trial/plan, activation + Calendly links, grace days, invoice outstanding/due date, new trial end) and renders three Email Templates seeded by `install.ensure_email_templates` (create-if-missing, then the business owns the copy): *Conta Suspensa* (copy branches on `cause`: trial expired → activation button; overdue → names the invoice and amount, "pay and access returns the same day"; manual → reply to us; all three: data intact, archive warning with the grace period), *Conta Arquivada* (full backup taken, restore on request), *Conta Reactivada* (site link; trial → new end date + activate link; signed → plan active). Hooks: `suspend(contract, reason, cause, invoice)` — rule 1 passes `cause="trial"`, rule 2 `cause="overdue", invoice=…`, the review queue `cause="overdue"`; `reactivate` and `archive` mail after their state change. Recipient `Contract.contact_email`, else `Customer.email_id`; no recipient → logged, never raised. Dry-run never mails because dry-run never calls the operations.

**Verified.** `test_tenant_lifecycle.py` (+4, in `TestLifecycleMail`): templates exist; suspended-trial mail carries the activation link and not the overdue wording; overdue mail names the invoice and carries no activation link; reactivated shows the new trial end and site URL; archived references the Contract; missing recipient returns False without sending. The roundtrip test asserts the hooks are called with the right kind and arguments.

### 2026-08-28 — Communication cycle rebuilt (validation → recommended cycle)

**Decisions (user).** Gate only `SMS Fatura Emitida` on `subscription` (Recibo and Nota de Crédito stay as they are); from the "unnecessary" list drop only `Lembrete 1`; build the rest of the recommended cycle.

**Built.**
- *Signup:* nurture set is now Dia 0, 3, 10, 20 (5, 15, 30 removed — 30 threatened an expiry that does not exist).
- *Trial:* new **`Trial - Primeira factura`** (Days After 2 from Contract `creation`): the 3-step first invoice, site button + Calendly; `Trial - 7 dias` not-logged-in branch offers to do the first invoice together; new **`SMS Trial Hoje`** (Days Before 0 `start_date`) to the new Contract field `mz_contact_mobile` (custom field, `allow_on_submit`; set at signup from the phone, updated by `/activar`, backfilled from Customer.mobile_no on migrate).
- *Activation:* new Email Template **`Conta Activada`** sent by `_activate` after the signature commits — plan, billing start, first-invoice date and payment methods; when the trial was suspended, `reactivate(notify=False)` so the customer gets one email, not two. `Pós-Contrato Dia 3/5/33`: the dead `/cloud-feedback` link replaced by the Calendly link and the copy adjusted.
- *Billing:* `Lembrete 1` (B+1) dropped; new **`SMS Vencimento Hoje`** (Days Before 0 `due_date`); `SMS Fatura Emitida` gated on `subscription`.
- *Overdue:* rhythm is now +1 `Aviso de Suspensão` → **+7 `Aviso de Atraso 7 dias` + SMS** → **+15 `Aviso de Atraso 15 dias`** → **+30 `Aviso de Suspensão em 3 dias` + SMS** (renamed from *Aviso de Desativação*; the "perda definitiva dos dados" claim removed — data stays intact, archive is 30 days after suspension) → engine suspends at +33 → *Conta Suspensa*.
- Fixture count 24 → 27 notifications (5 removed, 8 added); the removed ones deleted from erp.local.

**Verified.** `test_messaging.py` (+3): the five removed names are gone and the nurture set is exactly {0, 3, 10, 20}; the invoice SMS refuses a non-subscription invoice; no message links to `cloud-feedback`; the seven overdue/deadline notifications carry the expected anchor, fire on a subscription invoice, render without residue, SMS bodies name the invoice and stay short; `Primeira factura` and `SMS Trial Hoje` carry the right anchors and the SMS requires a mobile. `test_activation.py`: `_activate` sends exactly `("activated", contract)`. `test_tenant_lifecycle.py`: the *Conta Activada* render states plan, billing start and site URL. Full suite 106/106.

### 2026-08-28 — Provisioning failure: the lead is never told

**Decision (user).** No lead should know we had a problem provisioning their instance; the team is alerted to act fast, the customer is not.

**Built.** The retries-exhausted branch of `_handle_failure` that emailed the lead "Problema temporário com a sua conta" is removed. `_send_failure_alert` (every attempt, to `ops_alert_recipients`) now states that the lead was not informed and, when attempts are exhausted, prefixes the subject with **URGENTE** and says there are no more automatic retries — the human must fix and press "Tentar Novamente"; the lead then receives the normal delivery email. Row removed from `notifications_review.csv`; copy review updated.

**Verified.** `test_provisioning_retry` 4/4; syntax check (ruff not installed in this env).

### 2026-08-28 — Communication language applied to every customer message

**Decisions (user).** Relationship register: time-of-day greeting to the first name, "Com boas energias" signed by the account manager (Settings › default sales user, copied to `Contract.mz_account_manager` at signing), inbox cloud@. Formal register (billing/dunning): Prezado(a) + "Com os melhores cumprimentos", contacto@. CTA = the branded pill link, one per message. Mozambican Portuguese. Scheduled emails at 08:00. Billing documents untouched. Conta Arquivada promises 12 months to restore (AT retention is 10 years). SMS Vencimento Hoje sends people to the invoice email for bank details (replies to SMS are not seen).

**Built.** `utils/jinja.py` helpers; `Contract.mz_contact_name` + `mz_account_manager` (fixture, signup, backfill from primary Contact); `install.ensure_daily_alerts_hour` pins frappe's daily-alerts job to `0 8 * * *` after every migrate; standard blocks (`SUPPORT_LINE`, `TRIAL_PROMISE`, `CALL_OFFER`, `_LINK`) and `push_email_templates()`; every relationship message rewritten group by group (signup, delivery, trial, activation/pós-contrato, dunning register fixes, lifecycle) — see `docs/communication-copy-review.md` for the language and each message's objective. Live on erp.local (fields inserted, templates pushed, notifications updated in place).

**Verified.** Full `ai_saas` suite 111/111, incl. `TestCommunicationLanguage` (greeting by hour, signature fallback, cron pin, one voice across all non-billing messages, every message renders with a greeting).

### 2026-08-28 — Apps per segment; erpnext_mz installs without hrms

**Decisions (user).** Explicit app table per segment; base apps on every tenant = `erpnext` + `erpnext_mz`; segment apps installed one by one after site creation — a failing extra is logged and ops-alerted, provisioning continues.

**Built (ai_saas).** `provisioning.py`: `BASE_APPS`, `INSTALL_BEFORE_MZ = ("hrms",)` (rides on new-site ahead of erpnext_mz so its after_install sees payroll), `EXCLUDED_APPS`, `available_apps()` (sites/apps.txt), `apps_for_segment()`, whitelisted `get_apps_for_segment()`, `split_site_and_extra_apps()`; `_step_create_site` passes only site apps; new `_step_install_apps` (skips apps already on the site, isolates failures, `notify_ops`). `Segment Intelligence Map.aplicacoes` (Table → MZ Tenant App) seeded in the fixture: hrms where the segment lists "Recursos Humanos e Folha de Pagamento", pos_next+payments for retail/restauração/hotelaria/automóveis/distribuição, healthcare for saúde — sales edits from here. `Contract.mz_segment` (from `MZ Signup.industry`); signup builds the apps from it; the Contract client script fills the grid from the segment (and refills on change). `DEFAULT_APPS` is now an alias of the base.

**hrms gate (user, same day).** Every segment lists `hrms`; a tenant gets it only on a Profissional or Premium plan — `PLAN_GATED_APPS`, `plan_tier()` (tier word found in the Subscription Plan name, accent-insensitive, unknown → no gated app), `apps_for_segment(segment, plan)`; signup, provisioning and the Contract form pass the plan, and the grid refills when the plan changes.

**Built (erpnext_mz).** Employee/Salary Slip custom fields moved out of `fixtures/custom_field.json` into `setup/payroll_custom_fields.json`, applied by `install.ensure_payroll_custom_fields()` only when `hrms_installed()` (install + every migrate); fixture filter reduced to Company/Sales Order/Bank Account — on a no-hrms tenant `mz_is_proforma` and `mz_nib` now exist. `fix_salary_component_formula_policy` patch guarded. Known and left: HR/Payroll workspaces in `workspace.json` install with dead links on a no-hrms site.

**Verified.** `ai_saas` suite green incl. `test_provisioning_apps` (7) and the signup contract assertions; `erpnext_mz.tests.test_standalone` (5) + audit-field tests. Live on erp.local: doctype reloaded, `Contract-mz_segment` inserted, 25 app rows seeded, client script re-synced; `apps_for_segment("Saúde & Bem-Estar") == ["hrms","erpnext","erpnext_mz","healthcare"]`.

### 2026-08-28 — Signup as Guest: Address display rendering

First real signup on `/registo` failed at `_create_documents` with "O usuário Guest não tem acesso … Endereço": `party.set_customer_primaries` filled `Customer.primary_address` through `get_address_display(name)`, which loads the Address and runs `check_permission()` — the endpoint is guest-whitelisted, and Guest has no role on Address. Now `render_address(address, check_permissions=False)` (the Address was created by the same code path). The rollback in `_submit` had left nothing behind; a Failed signup without a Contract is retried on the next submit. `test_submit_creates_everything_and_provisions` now runs `_submit` as Guest so any permission check on the signup path fails in tests, not in production.
