"""Reactivation request page (G2/G3). Route: /reactivar?contract=<name>&token=<hmac>

Token validated before anything renders (the /activar posture). The page only asks;
the decision is a person's, on the MZ Overdue Review the request creates.
"""

import os

import frappe

no_cache = 1
no_breadcrumbs = 1


def get_context(context):
	from ai_saas.saas.activation import is_valid_token
	from ai_saas.saas.tenant_lifecycle import account_phase

	contract = (frappe.form_dict.get("contract") or "").strip()
	token = (frappe.form_dict.get("token") or "").strip()
	css = os.path.join(os.path.dirname(os.path.dirname(__file__)), "registo", "index.css")
	context.asset_version = int(os.path.getmtime(css)) if os.path.exists(css) else 0
	context.title = "Reactivar a minha conta — MozEconomia Cloud"
	context.contract_name = contract
	context.token = token
	context.valid = False

	if not is_valid_token(contract, token):
		return context
	c = frappe.db.get_value(
		"Contract", contract, ["party_name", "party_type", "docstatus", "mz_tenant_url"], as_dict=True
	)
	if not c or c.docstatus != 1 or c.party_type != "Customer":
		return context

	context.valid = True
	context.phase = account_phase(contract)
	context.customer_name = frappe.db.get_value("Customer", c.party_name, "customer_name") or c.party_name
	context.site_url = f"https://{c.mz_tenant_url}" if c.mz_tenant_url else ""
	context.pending = bool(frappe.db.exists(
		"MZ Overdue Review", {"contract": contract, "review_status": "Pending Review", "origin": "Pedido do Cliente"}
	))
	return context
