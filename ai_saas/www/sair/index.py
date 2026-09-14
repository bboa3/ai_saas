"""Unsubscribe page (content layers). Route: /sair?l=<lead>&t=<hmac>

A page with a button, not a one-click GET: mail scanners prefetch links, and a
prefetch must never unsubscribe anyone. The token is validated before anything renders.
"""

import os

import frappe

no_cache = 1
no_breadcrumbs = 1


def get_context(context):
	from ai_saas.saas.content import is_valid_unsubscribe_token

	lead = (frappe.form_dict.get("l") or "").strip()
	token = (frappe.form_dict.get("t") or "").strip()
	css = os.path.join(os.path.dirname(os.path.dirname(__file__)), "registo", "index.css")
	context.asset_version = int(os.path.getmtime(css)) if os.path.exists(css) else 0
	context.title = "Deixar de receber — MozEconomia Cloud"
	context.lead = lead
	context.token = token
	context.valid = is_valid_unsubscribe_token(lead, token) and bool(frappe.db.exists("Lead", lead))
	context.already = bool(context.valid and frappe.db.get_value("Lead", lead, "unsubscribed"))
	return context
