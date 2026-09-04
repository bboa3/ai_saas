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

from ai_saas.saas import crm
from ai_saas.saas.accounts import (
	NUIT_RE,
	STEP_FIELDS,
	_check_subdomain,
	_create_documents,
	_default_territory,
	_existing_contact,
	_find_duplicate,
	_resolve_city,
	_suggest_subdomain,
	_validate_step,
	create_account_from_desk,
	slug_from_company,
)
from ai_saas.saas.crm import enter_crm as _enter_crm
from ai_saas.saas.crm import sync_crm as _sync_crm
from ai_saas.saas.mz_address import CITY_PROVINCE
from ai_saas.saas.party import set_customer_primaries
from ai_saas.saas.provisioning import domain_for, domain_profile
from ai_saas.saas.settings import get_settings

PUBLIC_FIELDS = (
	"full_name", "email", "phone", "plan", "company_name", "tax_id", "tax_regime",
	"industry", "address", "city", "subdomain", "users", "terms_accepted", "current_step", "status", "mz_domain",
)


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
def start(full_name, email, phone=None, plan=None, domain=None):
	_limit(limit=10, seconds=600)                       # per IP
	# Every start opens a record, and a new record sends "Dia 0" (the resume link) to
	# that address: without a per-address cap, start() would be a mail cannon aimed at
	# anyone whose email is known. Five restarts an hour is far more than a human needs.
	_limit(identity=f"email:{(email or '').strip().lower()}", limit=5, seconds=3600)
	return _start(full_name, email, phone, plan, domain)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def update(token, step, data):
	_limit(limit=60, seconds=60)                        # per IP
	_limit(identity=f"token:{token}", limit=60, seconds=60)
	if isinstance(data, str):
		data = frappe.parse_json(data)
	return _update(token, cint(step), data or {})


@frappe.whitelist(allow_guest=True)
def suggest_subdomain(company_name, token=None):
	_limit(limit=60, seconds=60)
	# The browser rarely knows the city (derived server-side from the step-2 address);
	# the signup record does. An unknown token just means no city variant.
	city = frappe.db.get_value("MZ Signup", {"resume_token": token}, "city") if token else None
	return _suggest_subdomain(company_name, city=city)


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

def _start(full_name, email, phone=None, plan=None, domain=None):
	"""Step 1 — always a new record, always a token back.

	`domain` is the tenant domain the form is hard-coded to (a partner's, or MozEconomia's
	by default); the domain's profile may fix the sector, which the form then never asks.

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
	domain = domain_for(domain)
	industry = domain_profile(domain).get("segment")
	if industry and not frappe.db.exists("Segment Intelligence Map", industry):
		industry = None

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
		"mz_domain": domain, "industry": industry,
	})
	doc.insert(ignore_permissions=True)
	_supersede_other_live_signups(email, doc.name)
	if not completed:
		_enter_crm(doc)
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
	if step == 2 and domain_profile(doc.mz_domain).get("segment"):
		values.pop("industry", None)  # fixed by the form's domain, never the lead's choice
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
	_sync_crm(doc)
	frappe.db.commit()
	return {"state": "continue", "step": doc.current_step}


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

	# One submit creates one account (same row lock as the desk path): a concurrent
	# second POST waits here, then reads a status that is no longer Started.
	if frappe.db.get_value("MZ Signup", doc.name, "status", for_update=True) != "Started":
		return _state_payload(frappe.get_doc("MZ Signup", doc.name))
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


def _enforce_ceilings(doc):
	s = get_settings()
	from ai_saas.saas.tenant_lifecycle import live_trials

	trials = len(live_trials())
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
		payload["site_url"] = f"https://{doc.subdomain}{domain_for(doc.mz_domain)}"
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
