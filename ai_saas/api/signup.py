"""Signup API (docs/sales-funnel-implementation.md, A1-A4, A6).

Guest endpoints behind the /registo page. Every endpoint is a thin whitelisted
wrapper (rate-limited when a request exists) around a testable core function.
Access to an existing signup is only ever by its resume token; `start` is the
only call that works without one, and it answers identically whether or not the
email already has a signup (A2's one-live-signup rule): it opens a new record and
supersedes the older one, so nothing of an existing signup ever reaches the browser.
"""

import re

import frappe
from frappe.utils import add_days, cint, now_datetime, nowdate, validate_email_address

from ai_saas.saas.mz_address import CITY_PROVINCE
from ai_saas.saas.party import set_customer_primaries
from ai_saas.saas.tenant_lifecycle import get_settings

DOMAIN_SUFFIX = ".erp.mozeconomia.co.mz"
STEP_FIELDS = {
	1: ("full_name", "email", "phone", "plan"),
	2: ("company_name", "tax_id", "tax_regime", "industry", "address", "city"),
	3: ("subdomain", "plan", "terms_accepted"),
}
PUBLIC_FIELDS = (
	"full_name", "email", "phone", "plan", "company_name", "tax_id", "tax_regime",
	"industry", "address", "city", "subdomain", "terms_accepted", "current_step", "status",
)
NUIT_RE = re.compile(r"^\d{9}$")


# ---------------------------------------------------------------------------
# whitelisted wrappers
# ---------------------------------------------------------------------------

def _limit(identity=None, limit=10, seconds=60):
	"""Sliding-window rate limit on an identity WE choose — the client IP by default,
	or a value the endpoint received (the resume token). Frappe's rate_limit decorator
	keys on the raw request dict, which is not where frappe.call puts the arguments in
	every request shape; keying on the parsed argument is what actually holds.
	Only active when there is an HTTP request to limit."""
	if not getattr(frappe.local, "request", None):
		return
	identity = identity or frappe.local.request_ip or "no-ip"
	cache_key = f"rl:signup:{frappe.form_dict.get('cmd') or 'api'}:{identity}"
	cache = frappe.cache()
	count = cache.incr(cache_key)
	if count == 1:
		cache.expire(cache_key, seconds)
	if count > limit:
		frappe.throw("Demasiados pedidos. Aguarde um minuto e tente novamente.", frappe.RateLimitExceededError)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def start(full_name, email, phone=None, plan=None):
	_limit(limit=10, seconds=600)                       # per IP
	# Every start opens a record, and a new record sends "Dia 0" (the resume link) to
	# that address: without a per-address cap, start() would be a mail cannon aimed at
	# anyone whose email is known. Five restarts an hour is far more than a human needs.
	_limit(identity=f"email:{(email or '').strip().lower()}", limit=5, seconds=3600)
	return _start(full_name, email, phone, plan)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def update(token, step, data):
	_limit(limit=60, seconds=60)                        # per IP
	_limit(identity=f"token:{token}", limit=60, seconds=60)
	if isinstance(data, str):
		data = frappe.parse_json(data)
	return _update(token, cint(step), data or {})


@frappe.whitelist(allow_guest=True)
def suggest_subdomain(company_name):
	_limit(limit=60, seconds=60)
	return _suggest_subdomain(company_name)


@frappe.whitelist(allow_guest=True)
def check_subdomain(subdomain):
	_limit(limit=60, seconds=60)
	return _check_subdomain(subdomain)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def submit(token):
	_limit(limit=10, seconds=60)                        # per IP
	_limit(identity=f"token:{token}", limit=5, seconds=60)
	return _submit(token)


@frappe.whitelist(allow_guest=True)
def status(token):
	_limit(limit=120, seconds=60)                       # per IP
	_limit(identity=f"token:{token}", limit=120, seconds=60)
	return _status(token)


# ---------------------------------------------------------------------------
# core
# ---------------------------------------------------------------------------

def _start(full_name, email, phone=None, plan=None):
	"""Step 1 — always a new record, always a token back.

	The form never depends on the inbox: whoever is at the keyboard continues in
	this browser. What is never handed over is an *existing* record's token, since
	resuming echoes its fields back (company, NUIT): a second start for the same
	address opens a fresh record and supersedes the older live one, so exactly one
	signup per address is live and nurtured. The email link stays a helper — a way
	back in tomorrow or from another device — not a step of the form.

	An email with a Complete signup gets a record flagged duplicate_of_account,
	indistinguishable in the browser from a fresh signup until submit (A3), while
	the inbox gets "já tem uma conta".
	"""
	full_name = (full_name or "").strip()
	email = (email or "").strip().lower()
	if not full_name:
		frappe.throw("Indique o seu nome.")
	if not email or not validate_email_address(email):
		frappe.throw("Indique um email válido.")
	if plan and not frappe.db.exists("Subscription Plan", plan):
		plan = None

	completed = bool(frappe.db.exists("MZ Signup", {"email": email, "status": "Complete"}))
	# Someone hammering start() with a customer's email must not turn it into a mail flood:
	# one "já tem conta" email per address per 10 minutes.
	recently_told = completed and frappe.db.exists(
		"MZ Signup", {"email": email, "duplicate_of_account": 1,
		              "creation": (">", frappe.utils.add_to_date(now_datetime(), minutes=-10))}
	)
	doc = frappe.get_doc({
		"doctype": "MZ Signup", "email": email, "current_step": 2, "status": "Started",
		"full_name": full_name, "phone": (phone or "").strip(), "plan": plan,
		"step1_completed_on": now_datetime(), "duplicate_of_account": 1 if completed else 0,
	})
	doc.insert(ignore_permissions=True)
	_supersede_other_live_signups(email, doc.name)
	frappe.db.commit()
	if completed and not recently_told:
		_send_already_registered_email(doc)
		_alert_ops(
			f"Registo duplicado (email já com conta): {email}",
			f"O email {email} iniciou um novo registo ({doc.name}) mas já tem uma conta concluída.",
		)
	return {"token": doc.resume_token, "state": "continue", "step": 2}


def _update(token, step, data):
	doc = _load(token)
	if doc.status == "Superseded":
		# This browser holds the record's token, so it is the owner: continuing here
		# makes it the live one again (and retires whatever superseded it).
		doc.status = "Started"
		_supersede_other_live_signups(doc.email, doc.name)
	if doc.status != "Started":
		return _state_payload(doc)
	if step not in STEP_FIELDS:
		frappe.throw("Passo inválido.")

	values = {f: data.get(f) for f in STEP_FIELDS[step] if f in data}
	_validate_step(step, values)
	if step == 1 and values.get("email") and values["email"] != doc.email:
		_supersede_other_live_signups(values["email"], doc.name)
	address_before = doc.address
	doc.update(values)
	if step == 2:
		doc.step2_completed_on = now_datetime()
		city = _resolve_city(values.get("city"), doc.address)
		# Coming back to step 2 without touching the address keeps the city already
		# answered — the question is asked once, not on every pass through the step.
		doc.city = city or (doc.city if doc.address == address_before else "")
		if not doc.city:
			# One line was typed with no city we could recognise ("Av. 25 de Setembro"):
			# the Address needs one, so ask instead of guessing or crashing at submit.
			# What was typed is kept — the step is simply not finished yet.
			doc.save(ignore_permissions=True)
			frappe.db.commit()
			return {
				"state": "need_city", "step": 2,
				"message": "Falta a cidade neste endereço. Em que cidade fica a empresa?",
				"cities": sorted(CITY_PROVINCE),
			}
	doc.current_step = max(cint(doc.current_step) or 1, min(step + 1, 3))
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	return {"state": "continue", "step": doc.current_step}


def _resolve_city(typed, address):
	"""The city for the Billing Address: what the visitor answered when we had to ask,
	otherwise whatever the one-line address gives up. A town we do not list is taken as
	typed — our list is a convenience, not a gate."""
	from ai_saas.saas.mz_address import canonical_city, parse_mz_address

	typed = (typed or "").strip()
	if typed:
		return canonical_city(typed) or typed
	return parse_mz_address(address or "")["city"]


def _validate_step(step, values):
	if step == 1 and "email" in values:
		email = (values["email"] or "").strip().lower()
		if not validate_email_address(email):
			frappe.throw("Indique um email válido.")
		values["email"] = email
	if step == 2:
		if "company_name" in values and not (values["company_name"] or "").strip():
			frappe.throw("Indique o nome da empresa.")
		if "tax_id" in values:
			nuit = re.sub(r"\D", "", values["tax_id"] or "")
			if not NUIT_RE.match(nuit):
				frappe.throw("O NUIT tem 9 dígitos.")
			values["tax_id"] = nuit
		if values.get("industry") and not frappe.db.exists("Segment Intelligence Map", values["industry"]):
			frappe.throw("Sector inválido.")
		if "address" in values:
			if len((values["address"] or "").strip()) < 5:
				frappe.throw("Indique o endereço da empresa (rua, bairro, cidade).")
			values["address"] = values["address"].strip()
	if step == 3:
		if "subdomain" in values:
			check = _check_subdomain(values["subdomain"])
			if not check["available"]:
				frappe.throw(check["reason"])
			values["subdomain"] = check["subdomain"]
		if values.get("plan") and not frappe.db.exists("Subscription Plan", values["plan"]):
			frappe.throw("Plano inválido.")
		if "terms_accepted" in values:
			values["terms_accepted"] = cint(values["terms_accepted"])


# Legal forms and filler words that never belong in a subdomain.
_LEGAL_FORMS = {
	"lda", "ltda", "limitada", "sa", "s.a", "sarl", "ei", "unipessoal", "sociedade", "soc", "cia", "companhia",
	"empresa", "grupo", "holding", "mocambique", "mozambique", "moçambique", "mz",
}
_STOPWORDS = {"de", "da", "do", "das", "dos", "e", "&", "a", "o", "as", "os", "em", "para", "por", "com"}


def slug_from_company(company_name: str, max_words: int = 3) -> str:
	"""A subdomain people would actually type: 'Farmácia Central, Lda' -> 'farmacia-central',
	'João & Filhos Comércio Geral Limitada' -> 'joao-filhos-comercio'. Diacritics stripped,
	legal forms and connectives dropped, first `max_words` meaningful words, slug rules applied."""
	import unicodedata

	text = unicodedata.normalize("NFKD", company_name or "").encode("ascii", "ignore").decode().lower()
	words = [w for w in re.split(r"[^a-z0-9]+", text) if w]
	words = [w for w in words if w not in _LEGAL_FORMS and w not in _STOPWORDS and not (len(w) == 1 and w.isalpha())]
	slug = "-".join(words[:max_words])[:40].strip("-")
	if len(slug) < 3 and words:
		slug = "-".join(words)[:40].strip("-")
	return slug


def _suggest_subdomain(company_name):
	"""The first free variant of the company's natural slug: name, name-2, name-3…"""
	base = slug_from_company(company_name)
	if len(base) < 3:
		return {"subdomain": ""}
	for candidate in [base] + [f"{base}-{n}" for n in range(2, 20)]:
		if _check_subdomain(candidate)["available"]:
			return {"subdomain": candidate}
	return {"subdomain": ""}


def _check_subdomain(subdomain):
	from ai_saas.saas.provisioning import validate_slug

	slug = (subdomain or "").strip().lower()
	try:
		validate_slug(slug)
	except frappe.ValidationError as exc:
		return {"available": False, "subdomain": slug, "reason": str(exc)}
	taken = (
		frappe.db.exists("Contract", {"mz_tenant": slug, "docstatus": ("!=", 2)})
		or frappe.db.exists("MZ Tenant Provisioning", {"tenant_slug": slug})
		or frappe.db.exists("MZ Signup", {"subdomain": slug, "status": ("in", ["Submitted", "Provisioning", "Complete"])})
	)
	if taken:
		return {"available": False, "subdomain": slug, "reason": "Este endereço já está ocupado. Escolha outro."}
	return {"available": True, "subdomain": slug, "reason": ""}


def _submit(token):
	doc = _load(token)
	# A Failed signup whose documents were never created may be retried; one whose
	# Contract exists is the team's to fix (C1's retry) — the browser only polls.
	if doc.status == "Failed" and not doc.contract:
		doc.status = "Started"
	if doc.status != "Started":
		return _state_payload(doc)

	missing = [f for f in ("full_name", "email", "company_name", "tax_id", "tax_regime", "subdomain", "plan")
	           if not doc.get(f)]
	if missing or not cint(doc.terms_accepted):
		frappe.throw("Faltam dados para criar a conta. Verifique os passos anteriores.")

	check = _check_subdomain(doc.subdomain)
	if not check["available"]:
		frappe.throw(check["reason"])

	# A3 — duplicates against completed accounts: one generic message, the record
	# marked, the team told. Never a second account. A start-time match on a
	# Complete signup was only flagged, so the browser saw nothing until now.
	dup = _find_duplicate(doc)
	if not dup and cint(doc.duplicate_of_account):
		# Known person, new company: worth a word to sales, never a closed door.
		_alert_ops(
			f"Registo de um email que já tem conta: {doc.email}",
			f"{doc.name}: {doc.company_name} (NUIT {doc.tax_id}) — o email já concluiu um registo antes.",
		)
	if dup:
		doc.status = "Duplicate"
		doc.error = dup
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		_alert_ops(f"Registo duplicado: {doc.email} / NUIT {doc.tax_id}", f"{doc.name}: {dup}")
		frappe.throw(GENERIC_REFUSAL)

	_enforce_ceilings(doc)

	doc.status = "Submitted"
	doc.submitted_on = now_datetime()
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	try:
		_create_documents(doc)
	except Exception:
		frappe.db.rollback()
		doc.reload()
		doc.status = "Failed"
		doc.error = frappe.get_traceback()[-2000:]
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.log_error(title=f"AI SaaS: signup {doc.name} failed", message=frappe.get_traceback())
		_alert_ops(f"Falha na criação da conta: {doc.email}", f"MZ Signup {doc.name} — ver Error Log.")
		return _state_payload(doc)
	return _state_payload(doc)


GENERIC_REFUSAL = (
	"Não foi possível concluir o registo com estes dados. "
	"A nossa equipa vai verificar e entrar em contacto."
)


def _find_duplicate(doc):
	"""What must never exist twice is a **cloud account for the same company**, and the
	company is its NUIT.

	An email that already belongs to a Customer is *not* a duplicate. This is
	MozEconomia's own ERP: it holds every customer the business has ever had — on-prem
	licences, consulting, POS — and one person can own or represent more than one
	company. Refusing on `Customer.email_id` (the rule until 2026-08-25) shut the door
	on exactly the people most likely to buy; A4 now reuses that Customer instead.
	"""
	twin = frappe.db.sql(
		"""SELECT k.name FROM `tabContract` k INNER JOIN `tabCustomer` c ON c.name = k.party_name
		   WHERE k.docstatus = 1 AND IFNULL(k.mz_tenant, '') <> '' AND c.tax_id = %s LIMIT 1""",
		doc.tax_id,
	)
	if twin:
		return f"O NUIT {doc.tax_id} já tem uma conta cloud ({twin[0][0]})"
	other = frappe.db.get_value(
		"MZ Signup",
		{"name": ("!=", doc.name), "status": ("in", ["Submitted", "Provisioning", "Complete"]),
		 "tax_id": doc.tax_id},
		"name",
	)
	if other:
		return f"Outro registo com o NUIT {doc.tax_id} já foi submetido: {other}"
	return None


def _existing_contact(customer_name, email):
	"""A Contact of this customer that already carries this email, if there is one."""
	rows = frappe.get_all("Contact Email", filters={"email_id": email}, pluck="parent")
	if not rows:
		return None
	return frappe.db.get_value(
		"Dynamic Link",
		{"parenttype": "Contact", "parent": ("in", rows), "link_doctype": "Customer", "link_name": customer_name},
		"parent",
	)


def _enforce_ceilings(doc):
	s = get_settings()
	trials = frappe.db.count("Contract", {"docstatus": 1, "mz_account_phase": "Trial"})
	today_signups = frappe.db.count(
		"MZ Signup", {"submitted_on": (">=", nowdate()), "status": ("in", ["Submitted", "Provisioning", "Complete"])}
	)
	if trials >= s.max_concurrent_trials or today_signups >= s.max_signups_per_day:
		if doc.error != "ceiling":
			doc.db_set("error", "ceiling", update_modified=False)  # alert the team once, not per retry
			_alert_ops(
			"Limite de auto-serviço atingido",
				f"Trials activos: {trials}/{s.max_concurrent_trials}; registos hoje: "
				f"{today_signups}/{s.max_signups_per_day}. Registo em espera: {doc.name} ({doc.email}).",
			)
		frappe.throw(
			"Estamos com muita procura neste momento. O seu registo ficou guardado — "
			"volte a tentar mais tarde pela ligação que recebeu por email."
		)


def _create_documents(signup):
	"""A4: Lead, Opportunity, Customer (trial group), Contact, Billing Address,
	and the unsigned Contract — submitted, which provisions (B1)."""
	from erpnext.crm.doctype.contract_template.contract_template import get_contract_template

	from ai_saas.install import _CONTRACT_TEMPLATE_TITLE, TRIAL_CUSTOMER_GROUP
	from ai_saas.saas.provisioning import apps_for_segment
	from ai_saas.saas.contract_lifecycle import _get_company

	s = get_settings()
	company = _get_company()
	if not company:
		frappe.throw("Sem empresa por omissão configurada (Global Defaults).")

	# Lead
	lead_name = frappe.db.get_value("Lead", {"email_id": signup.email}, "name")
	if not lead_name:
		lead = frappe.get_doc({
			"doctype": "Lead", "first_name": signup.full_name, "company_name": signup.company_name,
			"email_id": signup.email, "mobile_no": signup.phone, "phone": signup.phone,
			"city": signup.city, "mz_segment": signup.industry, "status": "Lead",
		})
		lead.insert(ignore_permissions=True)
		lead_name = lead.name
	else:
		frappe.db.set_value("Lead", lead_name, {"company_name": signup.company_name, "mz_segment": signup.industry,
		                                       "city": signup.city, "mobile_no": signup.phone})

	# Opportunity — the record D2 advances and F3/F6 mark Lost
	opp = frappe.get_doc({
		"doctype": "Opportunity", "opportunity_from": "Lead", "party_name": lead_name, "company": company,
		"transaction_date": nowdate(), "sales_stage": "Cloud - Account Created",
		"contact_email": signup.email, "contact_mobile": signup.phone,
	})
	opp.insert(ignore_permissions=True)

	# Customer — the company is its NUIT. An existing customer of the house (on-prem,
	# consulting, POS) buying the cloud product is the same customer, not a second one:
	# reuse the record, keep its commercial group, and never overwrite what sales set.
	existing = frappe.db.get_value("Customer", {"tax_id": signup.tax_id}, "name") if signup.tax_id else None
	if existing:
		customer = frappe.get_doc("Customer", existing)
		if not customer.lead_name:
			frappe.db.set_value("Customer", customer.name, "lead_name", lead_name)
	else:
		customer = frappe.get_doc({
			"doctype": "Customer", "customer_name": signup.company_name, "customer_type": "Company",
			"customer_group": TRIAL_CUSTOMER_GROUP,   # so sales reporting can tell trials apart
			"territory": _default_territory(),
			"tax_id": signup.tax_id, "lead_name": lead_name, "mobile_no": signup.phone,
		})
		customer.insert(ignore_permissions=True)

	# Contact (primary) — the chain Customer → primary contact → email_id every invoice
	# email needs. Reused when this person is already a contact of this customer.
	contact_name = _existing_contact(customer.name, signup.email)
	if not contact_name:
		contact = frappe.get_doc({
			"doctype": "Contact", "first_name": signup.full_name, "is_primary_contact": 1,
			"email_ids": [{"email_id": signup.email, "is_primary": 1}],
			"phone_nos": [{"phone": signup.phone, "is_primary_mobile_no": 1}] if signup.phone else [],
			"links": [{"link_doctype": "Customer", "link_name": customer.name}],
		})
		contact.insert(ignore_permissions=True)
		contact_name = contact.name
	elif not frappe.db.get_value("Contact", contact_name, "is_primary_contact"):
		frappe.db.set_value("Contact", contact_name, "is_primary_contact", 1)

	# Billing Address — parsed from the one-line address the lead typed (mz_address);
	# marked primary and shipping, as ERPNext's own make_address marks a first address.
	# The customer can still correct the lines at activation (E1).
	from ai_saas.saas.mz_address import parse_mz_address

	address_name = None
	parts = parse_mz_address(signup.address or signup.city or "")
	line1 = parts["address_line1"] or parts["address_line2"] or signup.city
	city = signup.city or parts["city"]
	if line1 and city:
		address = frappe.get_doc({
			"doctype": "Address", "address_title": signup.company_name, "address_type": "Billing",
			"address_line1": line1, "address_line2": parts["address_line2"] if parts["address_line2"] != line1 else "",
			"city": city, "state": parts["state"] or CITY_PROVINCE.get(city, ""), "country": "Mozambique",
			"is_primary_address": 1, "is_shipping_address": 1,
			"links": [{"link_doctype": "Customer", "link_name": customer.name}],
		})
		address.insert(ignore_permissions=True)
		address_name = address.name
	# No usable address (both fields are mandatory on Address): the account is still
	# created — E1 collects the billing address at activation, before the first invoice.

	# The Customer's own pointers, so it leaves the funnel addressable: primary contact,
	# primary address, email and mobile. Nothing sales already filled in is overwritten.
	set_customer_primaries(customer, contact=contact_name, address=address_name,
	                       email=signup.email, mobile=signup.phone)

	# Contract — unsigned, submitted: the trial begins (B1)
	slug = signup.subdomain
	contract_fields = {
		"doctype": "Contract", "party_type": "Customer", "party_name": customer.name,
		"start_date": add_days(nowdate(), s.trial_length_days), "contract_template": _CONTRACT_TEMPLATE_TITLE,
		"mz_subscription_plan": signup.plan, "mz_tenant": slug, "mz_tenant_url": slug + DOMAIN_SUFFIX,
		"contact_email": signup.email, "mz_contact_name": signup.full_name, "mz_contact_mobile": signup.phone,
		"mz_segment": signup.industry, "mz_apps_to_install": [{"app_name": a} for a in apps_for_segment(signup.industry, signup.plan)],
	}
	rendered = get_contract_template(_CONTRACT_TEMPLATE_TITLE, contract_fields)
	contract_fields["contract_terms"] = (
		rendered["contract_terms"] if isinstance(rendered, dict) else rendered.contract_terms
	)
	contract = frappe.get_doc(contract_fields)
	contract.insert(ignore_permissions=True)
	# Link the documents BEFORE submit: provisioning commits inside on_submit, so a
	# failure after that point must still leave the signup pointing at what exists.
	signup.db_set({"lead": lead_name, "customer": customer.name, "contract": contract.name}, update_modified=False)
	contract.submit()

	prov = frappe.db.get_value("MZ Tenant Provisioning", {"contract": contract.name}, "name")
	if not prov:
		# provisioning was swallowed (contract_lifecycle logs it): never leave the page
		# polling forever — fail loudly and tell the team.
		signup.update({"provisioning": None, "status": "Failed",
		               "error": "Contrato submetido mas nenhum registo de provisionamento foi criado."})
		signup.save(ignore_permissions=True)
		frappe.db.commit()
		_alert_ops(f"Sem provisionamento após submissão: {signup.email}",
		           f"MZ Signup {signup.name}, Contract {contract.name}: ver Error Log.")
		return
	signup.update({"provisioning": prov, "status": "Provisioning"})
	signup.save(ignore_permissions=True)
	frappe.db.commit()


def _default_territory():
	"""Selling Settings default, else the root — translated on pt-MZ sites, so never assume its name."""
	return (
		frappe.db.get_single_value("Selling Settings", "territory")
		or frappe.db.get_value("Territory", {"is_group": 1, "parent_territory": ("in", ["", None])}, "name")
		or frappe.db.get_value("Territory", {}, "name", order_by="lft asc")
	)


def _status(token):
	"""Reconcile against the provisioning record — from Provisioning AND from Failed,
	so a C1 retry that succeeds turns the signup Complete."""
	doc = _load(token)
	if doc.status in ("Provisioning", "Failed") and doc.contract:
		prov = doc.provisioning or frappe.db.get_value("MZ Tenant Provisioning", {"contract": doc.contract}, "name")
		prov_status = frappe.db.get_value("MZ Tenant Provisioning", prov, "status") if prov else None
		target = {"Active": "Complete", "Failed": "Failed"}.get(prov_status)
		if target and (target != doc.status or prov != doc.provisioning):
			doc.db_set({"status": target, "provisioning": prov}, update_modified=False)
			frappe.db.commit()
	return _state_payload(doc)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _load(token):
	name = frappe.db.get_value("MZ Signup", {"resume_token": token or "__none__"}, "name")
	if not name:
		frappe.throw("Ligação de registo inválida ou expirada.", frappe.PermissionError)
	return frappe.get_doc("MZ Signup", name)


STATE_BY_STATUS = {
	"Started": "continue", "Superseded": "continue", "Submitted": "progress", "Provisioning": "progress",
	"Complete": "complete", "Failed": "failed", "Duplicate": "refused",
}


def _state_payload(doc, echo_fields=True):
	payload = {"state": STATE_BY_STATUS.get(doc.status, "continue"), "step": cint(doc.current_step) or 1}
	if doc.status in ("Started", "Superseded") and echo_fields:
		payload["fields"] = {f: doc.get(f) for f in PUBLIC_FIELDS}
	if doc.status in ("Provisioning", "Complete") and doc.subdomain:
		payload["site_url"] = f"https://{doc.subdomain}{DOMAIN_SUFFIX}"
	if doc.status == "Failed":
		payload["message"] = ("A criação da sua conta está a demorar mais do que o esperado. "
		                      "A nossa equipa já foi avisada e vai contactá-lo.")
	if doc.status == "Duplicate":
		payload["message"] = GENERIC_REFUSAL
	return payload


def _supersede_other_live_signups(email, keep):
	"""One live signup per address — the newest. The older records are kept for the
	funnel history but leave "Started", so the nurture notifications (and the resume
	link they carry) follow the record the person is actually filling in."""
	for name in frappe.get_all(
		"MZ Signup",
		filters={"email": email, "name": ("!=", keep), "status": "Started"},
		pluck="name",
	):
		frappe.db.set_value(
			"MZ Signup", name,
			{"status": "Superseded", "error": f"Substituído por {keep}"},
			update_modified=False,
		)


def _send_already_registered_email(doc):
	"""Objective: get them back into the account they already have — not stuck at a form."""
	from ai_saas.utils.jinja import mz_greeting, mz_signature

	try:
		site = frappe.db.get_value(
			"Contract", {"contact_email": doc.email, "mz_tenant_url": ("!=", ""), "docstatus": 1},
			"mz_tenant_url", order_by="creation desc",
		)
		where = (
			f'<p>A sua conta está em <a href="https://{site}" style="color:#008000;font-weight:bold">{site}</a>. '
			f"Se não se lembra da palavra-passe, use <em>Esqueci a palavra-passe</em> na página de entrada.</p>"
			if site else
			"<p>Se não se lembra do endereço ou da palavra-passe, responda a este email e ajudamos no próprio dia.</p>"
		)
		frappe.sendmail(
			recipients=[doc.email],
			subject="Já tem uma conta MozEconomia Cloud",
			message=(
				'<div style="font-family:Arial,Helvetica,sans-serif;max-width:600px;margin:0 auto;color:#020202">'
				f"<p>{mz_greeting(doc.full_name)}</p>"
				"<p>Este email já tem uma conta MozEconomia Cloud — não é preciso registar de novo.</p>"
				+ where +
				"<p>Se está a registar <strong>outra empresa</strong>, continue o registo que abriu — "
				"cada empresa tem a sua conta, pelo NUIT.</p>"
				'<p style="font-size:13px;color:#5a6270">Responda a este email ou fale connosco: '
				'<a href="mailto:cloud@mozeconomia.co.mz" style="color:#008000;font-weight:bold">cloud@mozeconomia.co.mz</a>'
				" · WhatsApp +258 87 4444 645</p>"
				+ mz_signature() + "</div>"
			),
			delayed=False,
		)
	except Exception:
		frappe.log_error(title=f"AI SaaS: already-registered email for {doc.name} not sent", message=frappe.get_traceback())


def _alert_ops(subject, message):
	from ai_saas.saas.alerts import notify_ops

	notify_ops(subject, f"<p>{frappe.utils.escape_html(message)}</p>")
