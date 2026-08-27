"""Customer-facing emails for the lifecycle acts: activated, suspended, archived, reactivated.

The copy lives in Email Templates (seeded once by install.ensure_email_templates,
then owned by the business); this module only assembles the context and sends.
Never raises: a lifecycle act that succeeded must not be undone by a mail problem.
"""

import frappe
from frappe.utils import fmt_money, formatdate

TEMPLATES = {
	"suspended": "MozEconomia Cloud - Conta Suspensa",
	"archived": "MozEconomia Cloud - Conta Arquivada",
	"reactivated": "MozEconomia Cloud - Conta Reactivada",
	"activated": "MozEconomia Cloud - Conta Activada",
}

HELP_EMAIL = "cloud@mozeconomia.co.mz"
HELP_WHATSAPP = "+258 87 4444 645"


def send_lifecycle_email(kind: str, contract_name: str, **extra) -> bool:
	"""Render TEMPLATES[kind] for the contract's contact and send. Returns whether it
	was sent; problems are logged, never raised."""
	try:
		context = build_context(contract_name, **extra)
		if not context["contact_email"]:
			frappe.log_error(
				title=f"AI SaaS: {kind} email for {contract_name} has no recipient",
				message="Contract.contact_email and the Customer's email are both empty.",
			)
			return False
		template = frappe.get_doc("Email Template", TEMPLATES[kind])
		frappe.sendmail(
			recipients=[context["contact_email"]],
			subject=frappe.render_template(template.subject, context),
			message=frappe.render_template(template.response_html or template.response, context),
			reference_doctype="Contract",
			reference_name=contract_name,
			delayed=False,
		)
		return True
	except Exception:
		frappe.log_error(title=f"AI SaaS: {kind} email for {contract_name} not sent", message=frappe.get_traceback())
		return False


def build_context(contract_name: str, **extra) -> dict:
	from ai_saas.saas.activation import get_activation_url
	from ai_saas.saas.provisioning import get_booking_url
	from ai_saas.saas.tenant_lifecycle import get_settings

	contract = frappe.db.get_value(
		"Contract", contract_name,
		["party_name", "contact_email", "is_signed", "start_date", "mz_subscription_plan", "mz_billing_start"],
		as_dict=True,
	)
	customer = frappe.db.get_value(
		"Customer", contract.party_name, ["customer_name", "email_id"], as_dict=True
	) or frappe._dict(customer_name=contract.party_name, email_id=None)
	prov = frappe.db.get_value(
		"MZ Tenant Provisioning", {"contract": contract_name}, ["site_name", "suspended_on"], as_dict=True
	) or frappe._dict()
	settings = get_settings()
	is_trial = not contract.is_signed

	context = {
		"customer_name": customer.customer_name or contract.party_name,
		"contact_email": contract.contact_email or customer.email_id or "",
		"site_name": prov.site_name or "",
		"site_url": f"https://{prov.site_name}" if prov.site_name else "",
		"is_trial": is_trial,
		"trial_end": formatdate(contract.start_date) if contract.start_date else "",
		"plan": contract.mz_subscription_plan or "",
		"billing_start": formatdate(contract.mz_billing_start) if contract.mz_billing_start else "",
		"activation_url": get_activation_url(contract_name) if is_trial else "",
		"booking_url": get_booking_url(),
		"grace_days": settings.grace_days_to_archive,
		"suspended_on": formatdate(prov.suspended_on) if prov.suspended_on else "",
		"help_email": HELP_EMAIL,
		"help_whatsapp": HELP_WHATSAPP,
		# act-specific, with safe blanks
		"cause": "manual",
		"invoice": "",
		"outstanding": "",
		"due_date": "",
		"new_trial_end": "",
	}
	if extra.get("invoice"):
		inv = frappe.db.get_value(
			"Sales Invoice", extra["invoice"], ["outstanding_amount", "currency", "due_date"], as_dict=True
		)
		if inv:
			context["outstanding"] = fmt_money(inv.outstanding_amount, currency=inv.currency)
			context["due_date"] = formatdate(inv.due_date)
	if extra.get("new_trial_end"):
		context["new_trial_end"] = formatdate(extra["new_trial_end"])
	context.update({k: v for k, v in extra.items() if k not in ("new_trial_end",)})
	return context
