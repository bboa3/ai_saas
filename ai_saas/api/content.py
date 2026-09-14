"""Guest endpoints of the content layers (docs/content-layers-implementation.md):
the Alerta Fiscal subscription, the guide-open beacon and the /sair unsubscribe.
Every one is a thin rate-limited wrapper around a core function, like api/signup.
Nothing here ever reveals whether an address already exists.
"""

import frappe
from frappe.utils import validate_email_address

from ai_saas.api.signup import _limit
from ai_saas.saas import content, crm

TAX_REGIMES = ("Normal (16%)", "Regime Especial/Reduzida", "Isento", "Não sei")


@frappe.whitelist(allow_guest=True, methods=["POST"])
def subscribe(full_name, email, company_name, segment, tax_regime=None):
	_limit(limit=10, seconds=600)  # per IP
	_limit(identity=f"email:{(email or '').strip().lower()}", limit=5, seconds=3600)
	return _subscribe(full_name, email, company_name, segment, tax_regime)


@frappe.whitelist(allow_guest=True)
def guide_opened(o, t):
	_limit(limit=60, seconds=60)
	return _guide_opened(o, t)


@frappe.whitelist(allow_guest=True, methods=["POST"])
def unsubscribe(l, t):
	_limit(limit=10, seconds=60)
	return _unsubscribe(l, t)


# ---------------------------------------------------------------------------
# core
# ---------------------------------------------------------------------------


def _subscribe(full_name, email, company_name, segment, tax_regime=None):
	"""Lead by email (one per address, the rule enter_crm keeps), flagged for the alert;
	an Opportunity at Subscribed with the guide when the person is not already being
	worked. A second subscription is a no-op that answers exactly like the first."""
	full_name = (full_name or "").strip()
	email = (email or "").strip().lower()
	company_name = (company_name or "").strip()
	segment = (segment or "").strip()
	tax_regime = (tax_regime or "").strip() or None
	if len(full_name) < 2:
		frappe.throw("Indique o seu nome.")
	if not email or not validate_email_address(email):
		frappe.throw("Indique um email válido, ex. nome@empresa.co.mz.")
	if len(company_name) < 2:
		frappe.throw("Indique o nome da empresa.")
	if not segment or not frappe.db.exists("Segment Intelligence Map", segment):
		frappe.throw("Escolha o sector de actividade.")
	if tax_regime and tax_regime not in TAX_REGIMES:
		frappe.throw("Regime de IVA desconhecido.")

	lead = frappe.db.get_value("Lead", {"email_id": email}, ["name", "unsubscribed"], as_dict=True)
	if lead:
		if lead.unsubscribed:
			# They asked to leave and are subscribing again by hand: that is consent.
			values = {"unsubscribed": 0, "status": "Lead"}
		else:
			values = {}
		values.update({"mz_fiscal_alerts": 1, "company_name": company_name, "mz_segment": segment})
		if tax_regime:
			values["mz_tax_regime"] = tax_regime
		if not frappe.db.get_value("Lead", lead.name, "source"):
			values["source"] = _source(content.SOURCE_ALERT)
		frappe.db.set_value("Lead", lead.name, values)
		lead_name = lead.name
	else:
		doc = frappe.get_doc(
			{
				"doctype": "Lead",
				"first_name": full_name,
				"email_id": email,
				"company_name": company_name,
				"mz_segment": segment,
				"mz_tax_regime": tax_regime,
				"mz_fiscal_alerts": 1,
				"source": _source(content.SOURCE_ALERT),
				"status": "Lead",
			}
		)
		doc.insert(ignore_permissions=True)
		lead_name = doc.name

	if not content.open_opportunity(lead_name):
		opportunity = content.create_opportunity(lead_name, crm.STAGE_SUBSCRIBED, content.SOURCE_ALERT)
		content.send_guide(opportunity)
	frappe.db.commit()
	return {"ok": 1}


def _guide_opened(o, t):
	"""The beacon from the guide page. Invalid or unknown → the same quiet answer."""
	o = (o or "").strip()
	if not content.is_valid_guide_token(o, (t or "").strip()) or not frappe.db.exists("Opportunity", o):
		return {"ok": 0}
	content.record_guide_open(o)
	frappe.db.commit()
	return {"ok": 1}


def _unsubscribe(l, t):
	l = (l or "").strip()
	if not content.is_valid_unsubscribe_token(l, (t or "").strip()):
		frappe.throw("Ligação inválida ou expirada.", frappe.PermissionError)
	content.suppress(l)
	frappe.db.commit()
	return {"ok": 1}


def _source(name):
	return name if frappe.db.exists("Lead Source", name) else None
