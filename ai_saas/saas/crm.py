"""The Opportunity is the funnel's ledger: every lifecycle event reports its stage
here, and every campaign (the Notifications on Opportunity) reads `sales_stage` and
`mz_stage_since`. Also: resolving the Opportunity behind a contract, and the sales ToDo.
"""

import json

import frappe
from frappe.utils import now_datetime

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

	Three lookups, in order: an Opportunity whose party is the contract's Customer;
	one whose party is the Lead the Customer was converted from (Customer.lead_name —
	the shape A4 produces); one carrying the Customer's name as customer_name.
	"""
	party_name = frappe.db.get_value("Contract", contract_name, "party_name")
	if not party_name:
		return None
	open_filter = {"status": ("not in", ["Lost", "Closed"])}

	candidates = [{"party_name": party_name}]
	customer = frappe.db.get_value("Customer", party_name, ["lead_name", "customer_name"], as_dict=True)
	if customer:
		if customer.lead_name:
			candidates.append({"opportunity_from": "Lead", "party_name": customer.lead_name})
		if customer.customer_name:
			candidates.append({"customer_name": customer.customer_name})

	for filters in candidates:
		name = frappe.db.get_value(
			"Opportunity", {**filters, **open_filter}, "name", order_by="creation desc"
		)
		if name:
			return name
	return None


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
		from ai_saas.saas.tenant_lifecycle import get_settings

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
