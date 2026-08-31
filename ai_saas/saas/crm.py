"""The Opportunity is the funnel's ledger: every lifecycle event reports its stage
here, and every campaign (the Notifications on Opportunity) reads `sales_stage` and
`mz_stage_since`. Also: resolving the Opportunity behind a contract, and the sales ToDo.
"""

import json

import frappe
from frappe.utils import now_datetime, nowdate

from ai_saas.saas.settings import get_settings

# The one lifecycle, in order. A stage is reported by the event that causes it
# (api/signup, contract_lifecycle, tenant_lifecycle, usage_signals) — never set by hand.
STAGE_FORM_STARTED = "Cloud - Form Started"
STAGE_ACCOUNT_CREATED = "Cloud - Account Created"
STAGE_TRIAL_ENGAGED = "Cloud - Trial Engaged"
STAGE_TRIAL_AT_RISK = "Cloud - Trial At Risk"
STAGE_TRIAL_EXPIRED = "Cloud - Trial Expired"
STAGE_ACTIVATED = "Cloud - Activated"
STAGE_SUSPENDED = "Cloud - Suspended"
STAGE_CLOSED = "Cloud - Closed"
STAGES = (
	STAGE_FORM_STARTED,
	STAGE_ACCOUNT_CREATED,
	STAGE_TRIAL_ENGAGED,
	STAGE_TRIAL_AT_RISK,
	STAGE_TRIAL_EXPIRED,
	STAGE_ACTIVATED,
	STAGE_SUSPENDED,
	STAGE_CLOSED,
)


def find_opportunity(contract_name):
	"""The live Opportunity behind a contract, or None (legacy contracts may have none).
	Converted counts as live: a signed account is still suspended, reactivated and
	archived, and each of those reports here. Only Lost / Closed are past.

	Two lookups, in order: an Opportunity whose party is the contract's Customer;
	one whose party is the Lead the Customer was converted from (Customer.lead_name —
	the shape A4 produces). Never by customer_name: a display name matches unrelated
	Opportunities anywhere in the ERP, and archive() would mark a stranger's deal Lost.
	The mirror resolver is find_contract().
	"""
	party_name = frappe.db.get_value("Contract", contract_name, "party_name")
	if not party_name:
		return None
	open_filter = {"status": ("not in", ["Lost", "Closed"])}

	candidates = [{"party_name": party_name}]
	lead = frappe.db.get_value("Customer", party_name, "lead_name")
	if lead:
		candidates.append({"opportunity_from": "Lead", "party_name": lead})

	for filters in candidates:
		name = frappe.db.get_value(
			"Opportunity", {**filters, **open_filter}, "name", order_by="creation desc"
		)
		if name:
			return name
	return None


def find_contract(opportunity) -> str | None:
	"""The cloud Contract behind an Opportunity — the mirror of find_opportunity(),
	derived on demand (never stored): via the Opportunity's MZ Signup when it has one
	(exact, the self-service shape), else party → Customer (a Customer party directly,
	a Lead party via Customer.lead_name) → that Customer's newest submitted Contract
	with a tenant. None for a non-cloud Opportunity. Exposed to notification templates
	as mz_find_contract — G2/G3 read the account only through this."""
	doc = opportunity
	if isinstance(opportunity, str):
		doc = frappe.db.get_value(
			"Opportunity", opportunity, ["opportunity_from", "party_name", "mz_signup"], as_dict=True
		)
	if not doc:
		return None
	if doc.get("mz_signup"):
		contract = frappe.db.get_value("MZ Signup", doc.mz_signup, "contract")
		if contract and frappe.db.exists("Contract", contract):
			return contract
	if doc.opportunity_from == "Customer":
		customer = doc.party_name
	elif doc.opportunity_from == "Lead":
		customer = frappe.db.get_value("Customer", {"lead_name": doc.party_name}, "name")
	else:
		customer = None
	if not customer:
		return None
	return frappe.db.get_value(
		"Contract",
		{"party_type": "Customer", "party_name": customer, "docstatus": 1, "mz_tenant": ("!=", "")},
		"name", order_by="creation desc",
	)


def report(opportunity, stage, status=None):
	"""Move the Opportunity to `stage`, stamping mz_stage_since — the anchor every
	campaign counts its days from. `status` is set only at the two true ends of the
	funnel (Converted on signature, Lost on archive): find_opportunity() excludes
	closed records, so anything still to be talked to must stay Open.
	Returns True if the stage actually changed."""
	if stage not in STAGES:
		frappe.throw(f"Etapa desconhecida: {stage}")
	values = {}
	current = frappe.db.get_value("Opportunity", opportunity, ["sales_stage", "status"], as_dict=True)
	if not current:
		return False
	if current.sales_stage != stage:
		values.update({"sales_stage": stage, "mz_stage_since": now_datetime()})
	if status and current.status != status:
		values["status"] = status
	if values:
		frappe.db.set_value("Opportunity", opportunity, values)
	return "sales_stage" in values


def report_for_contract(contract_name, stage, status=None):
	"""report() through the contract's Opportunity; a legacy contract without one is
	skipped silently. Returns the Opportunity name or None."""
	opportunity = find_opportunity(contract_name)
	if opportunity:
		report(opportunity, stage, status)
	return opportunity


def touch(opportunity):
	"""Activity without a stage change (a signup step saved): the cadence restarts."""
	frappe.db.set_value("Opportunity", opportunity, "mz_stage_since", now_datetime())


def create_sales_todo(opportunity, description, marker):
	"""A High ToDo on the Opportunity, allocated to its assignee or to
	MZ SaaS Settings.default_sales_user. `marker` dedups: one open ToDo per marker."""
	if frappe.db.exists(
		"ToDo",
		{
			"reference_type": "Opportunity",
			"reference_name": opportunity,
			"status": "Open",
			"description": ("like", f"{marker}%"),
		},
	):
		return None

	assignee = None
	raw_assign = frappe.db.get_value("Opportunity", opportunity, "_assign")
	if raw_assign:
		try:
			assignee = (json.loads(raw_assign) or [None])[0]
		except ValueError:
			assignee = None
	if not assignee:

		assignee = get_settings().default_sales_user
	if not assignee:
		from ai_saas.saas.alerts import notify_ops

		notify_ops(
			f"Sinal comercial sem responsável: {marker} {opportunity}",
			f"<p>{frappe.utils.escape_html(description)}</p><p>Defina 'Comercial por Omissão' em MZ SaaS Settings "
			f"ou atribua a Oportunidade {opportunity}.</p>",
		)

	todo = frappe.get_doc({
		"doctype": "ToDo",
		"status": "Open",
		"priority": "High",
		"date": frappe.utils.nowdate(),
		"reference_type": "Opportunity",
		"reference_name": opportunity,
		"allocated_to": assignee,
		"description": f"{marker} {description}",
	})
	todo.insert(ignore_permissions=True)
	_email_assignee(assignee, opportunity, marker, description)
	return todo.name


def _email_assignee(assignee, opportunity, marker, description):
	"""A ToDo inserted from code does not notify anyone (only desk assignment does),
	so the signal is also mailed to the person who owns it. Never raises."""
	if not assignee:
		return
	email = frappe.db.get_value("User", assignee, "email")
	if not email:
		return
	party = frappe.db.get_value("Opportunity", opportunity, "customer_name") or opportunity
	link = frappe.utils.get_url_to_form("Opportunity", opportunity)
	try:
		frappe.sendmail(
			recipients=[email],
			subject=f"[MozEconomia Cloud] {marker} {party}",
			message=(
				f"<p>{frappe.utils.escape_html(description)}</p>"
				f'<p><a href="{link}">Abrir a Oportunidade {opportunity}</a></p>'
				"<p style='font-size:12px;color:#5a6270'>Sinal do relatório de utilização dos trials. "
				"Um ToDo foi criado na Oportunidade com o mesmo texto.</p>"
			),
			reference_doctype="Opportunity",
			reference_name=opportunity,
			delayed=False,
		)
	except Exception:
		frappe.log_error(title=f"AI SaaS: signal email for {opportunity} not sent", message=frappe.get_traceback())


def enter_crm(signup, stage=None):
	"""Step 1 puts the person in the CRM: a Lead (one per email) and an Opportunity at
	"Form Started" — the record the nurture reads and Sales sees. A second start for the
	same address re-uses an Opportunity still at that stage (the resume link follows the
	live signup through mz_signup, and the Dia 0 email goes again with the new link);
	any other stage belongs to an earlier account, so a fresh Opportunity is opened.
	`stage` overrides the entry stage for a signup that skipped step 1's entry (a
	duplicate-of-account signup, or one started before the CRM entry existed): inserted
	straight at Account Created, it never matches the nurture's "New" trigger."""
	from ai_saas.saas.contract_lifecycle import _get_company

	lead_name = frappe.db.get_value("Lead", {"email_id": signup.email}, "name")
	if not lead_name:
		lead = frappe.get_doc({
			"doctype": "Lead", "first_name": signup.full_name, "email_id": signup.email,
			"mobile_no": signup.phone, "phone": signup.phone, "status": "Lead",
		})
		lead.insert(ignore_permissions=True)
		lead_name = lead.name

	opportunity = frappe.db.get_value(
		"Opportunity",
		{"opportunity_from": "Lead", "party_name": lead_name, "status": "Open",
		 "sales_stage": STAGE_FORM_STARTED},
		"name", order_by="creation desc",
	)
	if not opportunity:
		company = _get_company()
		if not company:
			frappe.throw("Sem empresa por omissão configurada (Global Defaults).")
		opp = frappe.get_doc({
			"doctype": "Opportunity", "opportunity_from": "Lead", "party_name": lead_name, "company": company,
			"transaction_date": nowdate(), "sales_stage": stage or STAGE_FORM_STARTED,
			"contact_email": signup.email, "contact_mobile": signup.phone,
			"mz_signup": signup.name, "mz_stage_since": now_datetime(),
		})
		opp.insert(ignore_permissions=True)
		opportunity = opp.name
	else:
		frappe.db.set_value("Opportunity", opportunity, {"mz_signup": signup.name, "contact_mobile": signup.phone})
		touch(opportunity)
		if stage is None:
			# Only a step-1 restart resends the welcome (the older resume link is dead).
			# Reached with a stage — a desk sale or a duplicate-account submit whose email
			# once started a web signup — the account is being created right now: no
			# "finish your registration" mail.
			_resend_day_zero(opportunity)
	signup.db_set({"lead": lead_name, "opportunity": opportunity}, update_modified=False)

def sync_crm(signup):
	"""What the form has learnt so far, onto the Lead; the Opportunity's clock restarts."""
	if not signup.opportunity:
		return
	frappe.db.set_value("Lead", signup.lead, {
		"first_name": signup.full_name, "company_name": signup.company_name or None,
		"mz_segment": signup.industry or None, "city": signup.city or None,
		"mobile_no": signup.phone or None, "phone": signup.phone or None,
	})
	frappe.db.set_value("Opportunity", signup.opportunity, {
		"contact_email": signup.email, "contact_mobile": signup.phone or None,
		"customer_name": signup.company_name or None,
	})
	touch(signup.opportunity)

def _resend_day_zero(opportunity):
	"""A restart supersedes the older signup, so its resume link is dead: send the
	"New"-event nurture again, now carrying the live one. Best effort."""
	name = "AI SaaS - Lead Nurture - Dia 0"
	if not frappe.db.get_value("Notification", name, "enabled"):
		return
	try:
		frappe.get_doc("Notification", name).send(frappe.get_doc("Opportunity", opportunity))
	except Exception:
		frappe.log_error(title="AI SaaS: Dia 0 resend failed", message=frappe.get_traceback())
