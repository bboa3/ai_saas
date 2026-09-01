"""Signup page (A7). Route: /registo[?plan=<Subscription Plan>][&token=<resume token>]

A resume token is validated in get_context BEFORE anything renders; only then are
the stored values put into the page. Without a token the page starts blank.

Partner forms (/registo-curati, /registo-kalenyholding) render this same template
through `build_context(context, domain)`: the tenant domain is hard-coded to the
form, everything else (plans, settings, funnel) is MozEconomia Cloud's.
"""

import frappe

from ai_saas.saas.provisioning import DEFAULT_DOMAIN, ROUTE_BY_DOMAIN, domain_for, domain_profile

no_cache = 1
no_breadcrumbs = 1

# What each form shows differently: who it is for and where the account lives.
FORMS = {
	DEFAULT_DOMAIN: {"brand": "MozEconomia Cloud", "partner": ""},
	# Curati's own look: full logo (icon + wordmark), Curati green (css .reg--curati)
	".erp.curati.co.mz": {"brand": "Curati", "partner": "Curati Saúde, LDA",
	                      "theme": "curati", "logo": "/assets/ai_saas/images/curati-logo.png"},
	# Kaleny's own look: icon + "Kaleny Holding" wordmark, brand blue (css .reg--kaleny)
	".erp.kalenyholding.com": {"brand": "Kaleny Holding", "partner": "Kaleny Holding, SU, SA",
	                            "theme": "kaleny", "icon": "/assets/ai_saas/images/kaleny-icon.png",
	                            "tagline": "Holding Company"},
}


def get_context(context):
	return build_context(context, DEFAULT_DOMAIN)


def build_context(context, domain):
	import os

	from ai_saas.api.signup import _state_payload

	domain = domain_for(domain)
	form = FORMS[domain]
	context.domain = domain
	context.route = ROUTE_BY_DOMAIN[domain]
	context.partner = form["partner"]
	context.brand = form["brand"]
	context.theme = form.get("theme") or ""
	context.brand_icon = form.get("icon") or ""
	context.brand_logo = form.get("logo") or ""
	context.brand_tagline = form.get("tagline") or ""
	context.preset_industry = domain_profile(domain).get("segment") or ""
	context.title = "Criar a minha conta — " + form["brand"]
	here = os.path.dirname(os.path.abspath(__file__))
	context.asset_version = int(max(os.path.getmtime(os.path.join(here, f)) for f in ("index.js", "index.css")))
	from ai_saas.saas.activation import cloud_plans

	context.plans = cloud_plans()
	context.industries = frappe.get_all("Segment Intelligence Map", pluck="name", order_by="name")
	from ai_saas.saas.mz_address import CITY_PROVINCE

	context.cities = sorted(CITY_PROVINCE)
	context.tax_regimes = ["Normal (16%)", "Regime Especial/Reduzida", "Isento", "Não sei"]
	context.trial_days = frappe.db.get_single_value("MZ SaaS Settings", "trial_length_days") or 14
	from ai_saas.saas.settings import get_settings

	context.minimum_users = get_settings().minimum_users

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
