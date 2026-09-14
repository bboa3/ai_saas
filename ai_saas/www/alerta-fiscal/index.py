"""Alerta Fiscal subscription page (content layer 1). Route: /alerta-fiscal

The page of the Meta ads: it sells the alert, never the software. Same posture as
/registo — no frappe.call, inline errors, the shared stylesheet — and one guest
endpoint behind it, api/content.subscribe.
"""

import os

import frappe

from ai_saas.saas.settings import get_settings

no_cache = 1
no_breadcrumbs = 1

TAX_REGIMES = ["Normal (16%)", "Regime Especial/Reduzida", "Isento", "Não sei"]


def get_context(context):
	here = os.path.dirname(os.path.abspath(__file__))
	css = os.path.join(os.path.dirname(here), "registo", "index.css")
	js = os.path.join(here, "index.js")
	context.asset_version = int(max(os.path.getmtime(p) for p in (css, js) if os.path.exists(p)) or 0)
	context.title = "Alerta Fiscal — MozEconomia Cloud"
	context.industries = frappe.get_all("Segment Intelligence Map", pluck="name", order_by="name")
	context.tax_regimes = TAX_REGIMES
	context.lead_days = get_settings().fiscal_alert_lead_days
	context.meta_pixel_id = get_settings().meta_pixel_id
	context.obligations = frappe.get_all(
		"MZ Fiscal Obligation", {"enabled": 1}, ["title", "periodicity"], order_by="title"
	)
	return context
