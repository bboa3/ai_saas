"""A customer asking for their account back (docs/sales-funnel-implementation.md, G2/G3).

The request is authenticated by the contract's activation token — the same HMAC the
activation link carries, so only someone holding an email we sent can raise it — and
lands as an MZ Overdue Review with origin "Pedido do Cliente", assigned to the account
manager. Nothing is reactivated here: a person decides on the review, and that record
is the audit trail. Restore of an archived site is a manual act by the team.
"""

import frappe
from frappe.utils import now_datetime

from ai_saas.api.signup import _limit
from ai_saas.saas.activation import is_valid_token
from ai_saas.saas.alerts import notify_ops
from ai_saas.saas.tenant_lifecycle import account_phase, get_settings

MAX_MESSAGE = 1000


@frappe.whitelist(allow_guest=True, methods=["POST"])
def request(contract, token, message=None):
	_limit(limit=10, seconds=600)  # per IP
	_limit(identity=f"contract:{contract}", limit=3, seconds=3600)
	return _request(contract, token, message)


def _request(contract, token, message=None):
	if not is_valid_token(contract, token):
		frappe.throw("Ligação inválida ou expirada.", frappe.PermissionError)
	c = frappe.db.get_value(
		"Contract", contract, ["name", "party_name", "party_type", "docstatus", "mz_account_manager"], as_dict=True
	)
	if not c or c.docstatus != 1 or c.party_type != "Customer":
		frappe.throw("Ligação inválida ou expirada.", frappe.PermissionError)

	phase = account_phase(contract)
	if phase not in ("Suspended", "Closed"):
		return {"state": "active", "phase": phase}

	message = (message or "").strip()[:MAX_MESSAGE]
	stamp = now_datetime().strftime("%Y-%m-%d %H:%M")
	line = f"[{stamp}] Pedido de reactivação do cliente" + (f": {message}" if message else ".")
	assignee = c.mz_account_manager or get_settings().default_sales_user or None

	existing = frappe.db.get_value(
		"MZ Overdue Review", {"contract": contract, "review_status": "Pending Review"}, ["name", "notes"], as_dict=True
	)
	if existing:
		frappe.db.set_value(
			"MZ Overdue Review", existing.name,
			{"notes": f"{existing.notes or ''}\n{line}".strip(), "origin": "Pedido do Cliente"},
		)
		review = existing.name
	else:
		doc = frappe.get_doc({
			"doctype": "MZ Overdue Review", "customer": c.party_name, "contract": contract,
			"origin": "Pedido do Cliente", "review_status": "Pending Review",
			"assigned_to": assignee, "notes": line,
		})
		doc.insert(ignore_permissions=True)
		review = doc.name
		_assign(review, assignee)
	frappe.db.commit()
	notify_ops(
		f"Pedido de reactivação: {c.party_name} ({phase})",
		f"<p>{frappe.utils.escape_html(line)}</p>"
		f'<p><a href="{frappe.utils.get_url_to_form("MZ Overdue Review", review)}">Abrir a revisão {review}</a></p>',
	)
	return {"state": "requested", "phase": phase, "review": review}


def _assign(review, assignee):
	"""A desk assignment (ToDo + notification to the person), so the request is seen."""
	if not assignee:
		return
	from frappe.desk.form.assign_to import add

	try:
		add({
			"assign_to": [assignee], "doctype": "MZ Overdue Review", "name": review,
			"description": "Pedido de reactivação do cliente — decidir na revisão.", "priority": "High",
		}, ignore_permissions=True)
	except Exception:
		frappe.log_error(title=f"AI SaaS: assignment of {review} failed", message=frappe.get_traceback())
