"""Activation page (E1). Route: /activar?contract=<name>&token=<hmac>

Token validated in get_context before anything renders — the qr_validation posture.
"""

import frappe

no_cache = 1
no_breadcrumbs = 1


def get_context(context):
	from ai_saas.saas.activation import get_activation_context

	contract = (frappe.form_dict.get("contract") or "").strip()
	token = (frappe.form_dict.get("token") or "").strip()
	import os

	# Same stylesheet as /registo, cache-busted by its own mtime (the page has no assets
	# of its own, so the version it asks for is the one /registo publishes).
	css = os.path.join(os.path.dirname(__file__), "registo", "index.css")
	context.asset_version = int(os.path.getmtime(css)) if os.path.exists(css) else 0

	ctx = get_activation_context(contract, token)
	context.update(ctx)
	context.contract_name = contract
	context.title = "Activar a minha conta — MozEconomia Cloud"
	return context
