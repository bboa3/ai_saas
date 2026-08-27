"""Signup page (A7). Route: /registo[?plan=<Subscription Plan>][&token=<resume token>]

A resume token is validated in get_context BEFORE anything renders; only then are
the stored values put into the page. Without a token the page starts blank.
"""

import frappe

no_cache = 1
no_breadcrumbs = 1


def get_context(context):
	from ai_saas.api.signup import _state_payload

	import os

	context.title = "Criar a minha conta — MozEconomia Cloud"
	here = os.path.dirname(__file__)
	context.asset_version = int(max(os.path.getmtime(os.path.join(here, f)) for f in ("index.js", "index.css")))
	from ai_saas.saas.activation import cloud_plans

	context.plans = cloud_plans()
	context.industries = frappe.get_all("Segment Intelligence Map", pluck="name", order_by="name")
	from ai_saas.saas.mz_address import CITY_PROVINCE

	context.cities = sorted(CITY_PROVINCE)
	context.tax_regimes = ["Normal (16%)", "Regime Especial/Reduzida", "Isento", "Não sei"]
	context.trial_days = frappe.db.get_single_value("MZ SaaS Settings", "trial_length_days") or 14

	plan = (frappe.form_dict.get("plan") or "").strip()
	context.preselected_plan = plan if any(p.name == plan for p in context.plans) else (
		context.plans[0].name if context.plans else ""
	)

	token = (frappe.form_dict.get("token") or "").strip()
	context.resume = None
	context.token = ""
	if token:
		name = frappe.db.get_value("MZ Signup", {"resume_token": token}, "name")
		if name:
			doc = frappe.get_doc("MZ Signup", name)
			context.resume = _state_payload(doc)
			context.token = token
	return context
