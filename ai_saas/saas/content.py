"""Content layers 2 and 3 — the sector guide and the auxiliary emails
(docs/content-layers-implementation.md).

Layer 2: the guide is a native Web Page the team writes; the only code-side link is
the page's own "sectors it serves" table (Web Page.mz_segments, child MZ Guide Segment). Every delivery carries a per-company URL — the
Opportunity's name signed with the same HMAC activation.py uses — and opening it is
the one signal the system sees before the form: it moves the Opportunity to Aware.

Layer 3: `send_auxiliary` mails an Email Template to every live content-stage
Opportunity; silence through N of them (MZ SaaS Settings) moves the contact to
Dormant, where only the Alerta Fiscal still arrives. Suppression (`suppress`) is
stage-based plus Lead.unsubscribed — never a global unsubscribe, which would also
silence invoices if the prospect later becomes a customer.
"""

import frappe
from erpnext_mz.qr_code.qr_generator import _generate_validation_hash, validate_document_hash
from frappe.utils import cint, get_url, now_datetime, nowdate

from ai_saas.saas import crm
from ai_saas.saas.settings import GUIDE_EMAIL_TEMPLATE, get_settings

SOURCE_ALERT = "Alerta Fiscal"
SOURCE_GUIDE = "Guia Sectorial"
SOURCE_IMPORT = "Importação"
LEAD_SOURCES = (SOURCE_ALERT, SOURCE_GUIDE, SOURCE_IMPORT)


# ---------------------------------------------------------------------------
# tokens + urls (exposed to Jinja through utils/jinja.py)
# ---------------------------------------------------------------------------


def guide_token(opportunity: str) -> str:
	return _generate_validation_hash("Opportunity", opportunity)


def is_valid_guide_token(opportunity: str, token: str) -> bool:
	return bool(opportunity) and validate_document_hash("Opportunity", opportunity, token or "")


def unsubscribe_token(lead: str) -> str:
	return _generate_validation_hash("Lead", lead)


def is_valid_unsubscribe_token(lead: str, token: str) -> bool:
	return bool(lead) and validate_document_hash("Lead", lead, token or "")


def unsubscribe_url(lead: str) -> str:
	"""The /sair page for a Lead — in every content email (alert, guide, auxiliary)."""
	return get_url(f"/sair?l={lead}&t={unsubscribe_token(lead)}")


def guide_page_for(opportunity) -> dict | None:
	"""The published Web Page behind the Opportunity's Lead segment, or None."""
	doc = (
		opportunity
		if not isinstance(opportunity, str)
		else frappe.db.get_value(
			"Opportunity", opportunity, ["name", "opportunity_from", "party_name"], as_dict=True
		)
	)
	if not doc or doc.opportunity_from != "Lead":
		return None
	segment = frappe.db.get_value("Lead", doc.party_name, "mz_segment")
	if not segment:
		return None
	pages = frappe.get_all(
		"MZ Guide Segment", {"parenttype": "Web Page", "segment": segment}, pluck="parent", distinct=True
	)
	if not pages:
		return None
	row = frappe.db.get_value(
		"Web Page",
		{"name": ("in", pages), "published": 1, "route": ("!=", "")},
		["name", "route", "title", "meta_description"],
		as_dict=True,
		order_by="modified desc",
	)
	return row or None


def guide_url(opportunity: str) -> str:
	"""Per-company guide link: the page's own route + the signed Opportunity. '' when
	the segment has no published guide."""
	page = guide_page_for(opportunity)
	if not page:
		return ""
	route = page.route if page.route.startswith("/") else "/" + page.route
	return get_url(f"{route}?o={opportunity}&t={guide_token(opportunity)}")


# ---------------------------------------------------------------------------
# the Opportunity behind a Lead
# ---------------------------------------------------------------------------


def open_opportunity(lead: str) -> dict | None:
	"""The Lead's newest Open Opportunity at any stage (a person is worked once)."""
	return frappe.db.get_value(
		"Opportunity",
		{"opportunity_from": "Lead", "party_name": lead, "status": "Open"},
		["name", "sales_stage"],
		order_by="creation desc",
		as_dict=True,
	)


def create_opportunity(lead: str, stage: str, source: str | None = None) -> str:
	"""A content-stage Opportunity for a Lead, the shape enter_crm creates for the form."""
	from ai_saas.saas.contract_lifecycle import _get_company

	company = _get_company()
	if not company:
		frappe.throw("Sem empresa por omissão configurada (Global Defaults).")
	lead_doc = frappe.db.get_value("Lead", lead, ["email_id", "mobile_no", "company_name"], as_dict=True)
	opp = frappe.get_doc(
		{
			"doctype": "Opportunity",
			"opportunity_from": "Lead",
			"party_name": lead,
			"company": company,
			"transaction_date": nowdate(),
			"sales_stage": stage,
			"mz_stage_since": now_datetime(),
			"contact_email": lead_doc.email_id,
			"contact_mobile": lead_doc.mobile_no,
			"customer_name": lead_doc.company_name,
			"source": source if source and frappe.db.exists("Lead Source", source) else None,
		}
	)
	opp.insert(ignore_permissions=True)
	return opp.name


# ---------------------------------------------------------------------------
# layer 2 — the guide
# ---------------------------------------------------------------------------


def send_guide(opportunity: str) -> bool:
	"""Day 0: the delivery email — sector in the subject, three lines, the link, nothing
	else. Silent (False) when the segment has no published guide. Stamps
	mz_guide_sent_on, the anchor of the Day-4 notification. Never raises."""
	page = guide_page_for(opportunity)
	if not page:
		return False
	opp = frappe.db.get_value(
		"Opportunity", opportunity, ["name", "party_name", "contact_email", "customer_name"], as_dict=True
	)
	if not opp or not opp.contact_email:
		return False
	lead = (
		frappe.db.get_value("Lead", opp.party_name, ["first_name", "lead_name", "mz_segment"], as_dict=True)
		or frappe._dict()
	)
	if not frappe.db.exists("Email Template", GUIDE_EMAIL_TEMPLATE):
		frappe.log_error(title="AI SaaS: guide email template missing", message=GUIDE_EMAIL_TEMPLATE)
		return False
	from ai_saas.utils.jinja import mz_greeting, mz_signature

	context = {
		"greeting": mz_greeting(lead.first_name or lead.lead_name),
		"signature": mz_signature(),
		"company_name": opp.customer_name or "",
		"segment": lead.mz_segment or "",
		"guide_title": page.title or "",
		"guide_summary": page.meta_description or "",
		"guide_url": guide_url(opportunity),
		"unsubscribe_url": unsubscribe_url(opp.party_name),
	}
	mail = frappe.get_doc("Email Template", GUIDE_EMAIL_TEMPLATE).get_formatted_email(context)
	try:
		frappe.sendmail(
			recipients=[opp.contact_email],
			subject=mail["subject"],
			message=mail["message"],
			reference_doctype="Opportunity",
			reference_name=opportunity,
			delayed=False,
		)
	except Exception:
		frappe.log_error(
			title=f"AI SaaS: guide email for {opportunity} not sent", message=frappe.get_traceback()
		)
		return False
	frappe.db.set_value("Opportunity", opportunity, "mz_guide_sent_on", nowdate(), update_modified=False)
	return True


def record_guide_open(opportunity: str) -> str | None:
	"""The guide link was opened. Subscribed / Dormant → Aware; Aware → the clock
	restarts; past the form nothing changes. Returns the stage after the open."""
	current = frappe.db.get_value(
		"Opportunity", opportunity, ["sales_stage", "mz_guide_opened_on"], as_dict=True
	)
	if not current:
		return None
	now = now_datetime()
	values = {"mz_guide_last_opened": now}
	if not current.mz_guide_opened_on:
		values["mz_guide_opened_on"] = now
	frappe.db.set_value("Opportunity", opportunity, values, update_modified=False)
	if current.sales_stage in (crm.STAGE_SUBSCRIBED, crm.STAGE_DORMANT):
		crm.report(opportunity, crm.STAGE_AWARE)
		return crm.STAGE_AWARE
	if current.sales_stage == crm.STAGE_AWARE:
		crm.touch(opportunity)
		frappe.db.set_value("Opportunity", opportunity, "mz_aux_sent", 0, update_modified=False)
	return current.sales_stage


@frappe.whitelist()
def enroll(lead: str, source: str = SOURCE_IMPORT) -> str:
	"""Desk button on a Lead (imports): put the person in the base and send the guide.
	A Lead already worked (an Open Opportunity at any stage) is left alone."""
	frappe.only_for(("System Manager", "Sales Manager", "Sales User"))
	if not frappe.db.exists("Lead", lead):
		frappe.throw("Lead inexistente.")
	if not frappe.db.get_value("Lead", lead, "email_id"):
		frappe.throw("O Lead não tem email — sem email não há guia.")
	existing = open_opportunity(lead)
	if existing:
		frappe.msgprint(
			f"Já existe a Oportunidade {existing.name} ({existing.sales_stage}); nada foi enviado."
		)
		return existing.name
	if not frappe.db.get_value("Lead", lead, "source"):
		frappe.db.set_value(
			"Lead", lead, "source", source if frappe.db.exists("Lead Source", source) else None
		)
	opportunity = create_opportunity(lead, crm.STAGE_SUBSCRIBED, source)
	sent = send_guide(opportunity)
	frappe.msgprint(
		f"Oportunidade {opportunity} criada em {crm.STAGE_SUBSCRIBED}. "
		+ ("Guia sectorial enviado." if sent else "Sem guia publicado para o sector — nada foi enviado.")
	)
	return opportunity


# ---------------------------------------------------------------------------
# layer 3 — auxiliary emails
# ---------------------------------------------------------------------------


def auxiliary_recipients() -> list:
	"""Live content-stage Opportunities whose Lead still wants mail."""
	return frappe.db.sql(
		"""
		SELECT o.name AS opportunity, o.sales_stage, o.mz_aux_sent, o.customer_name,
		       l.name AS lead, l.email_id, l.first_name, l.lead_name, l.company_name, l.mz_segment
		FROM `tabOpportunity` o
		JOIN `tabLead` l ON l.name = o.party_name
		WHERE o.opportunity_from = 'Lead' AND o.status = 'Open'
		  AND o.sales_stage IN (%s, %s)
		  AND IFNULL(l.unsubscribed, 0) = 0 AND IFNULL(l.email_id, '') != ''
		ORDER BY o.creation
		""",
		(crm.STAGE_SUBSCRIBED, crm.STAGE_AWARE),
		as_dict=True,
	)


@frappe.whitelist()
def send_auxiliary(email_template: str, dry_run=0) -> dict:
	"""Send one Email Template to every auxiliary recipient, once. The template's Jinja
	sees `doc` (the Lead row), `opportunity`, `greeting`, `signature`, `unsubscribe_url`.
	Each send counts on the Opportunity; reaching the cap reports Dormant. `dry_run`
	sends nothing and returns who would receive it."""
	frappe.only_for(("System Manager", "Sales Manager"))
	if not frappe.db.exists("Email Template", email_template):
		frappe.throw(f"Email Template inexistente: {email_template}")
	template = frappe.get_doc("Email Template", email_template)
	cap = get_settings().auxiliary_max_without_signal
	rows = auxiliary_recipients()
	result = {
		"template": email_template,
		"dry_run": cint(dry_run),
		"recipients": [r.email_id for r in rows],
		"sent": 0,
		"dormant": [],
	}
	if cint(dry_run):
		return result
	from ai_saas.utils.jinja import mz_greeting, mz_signature

	signature = mz_signature()
	for r in rows:
		context = {
			"doc": r,
			"opportunity": r.opportunity,
			"greeting": mz_greeting(r.first_name or r.lead_name),
			"signature": signature,
			"unsubscribe_url": unsubscribe_url(r.lead),
		}
		mail = template.get_formatted_email(context)
		try:
			frappe.sendmail(
				recipients=[r.email_id],
				subject=mail["subject"],
				message=mail["message"],
				reference_doctype="Opportunity",
				reference_name=r.opportunity,
			)
		except Exception:
			frappe.log_error(
				title=f"AI SaaS: auxiliary email for {r.opportunity} not sent", message=frappe.get_traceback()
			)
			continue
		result["sent"] += 1
		count = cint(r.mz_aux_sent) + 1
		frappe.db.set_value("Opportunity", r.opportunity, "mz_aux_sent", count, update_modified=False)
		if count >= cap:
			crm.report(r.opportunity, crm.STAGE_DORMANT)
			result["dormant"].append(r.opportunity)
	return result


# ---------------------------------------------------------------------------
# suppression
# ---------------------------------------------------------------------------


def suppress(lead: str) -> None:
	"""The person asked to leave: no more content mail of any kind. The Lead's native
	unsubscribed flag is what every sender here checks; a content-stage Opportunity is
	closed as Unsubscribed / Lost. An account in trial or paying keeps its funnel stage
	and its transactional mail — only the content stops."""
	if not frappe.db.exists("Lead", lead):
		return
	frappe.db.set_value("Lead", lead, {"unsubscribed": 1, "mz_fiscal_alerts": 0, "status": "Do Not Contact"})
	existing = open_opportunity(lead)
	if existing and existing.sales_stage in crm.CONTENT_STAGES:
		crm.report(existing.name, crm.STAGE_UNSUBSCRIBED, status="Lost")
