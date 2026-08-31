"""Activation — signing is activating (docs/sales-funnel-implementation.md, E1).

The customer converts alone, at any hour, on /activar: a token-gated page that
asks for no new choices — it shows the plan chosen at signup and the billing
details on file, lets them be corrected, completes the billing address, and on
confirmation signs the contract THROUGH THE DOCUMENT SAVE PATH. That last point
is the whole item: frappe.db.set_value fires no doc_events, so B1's
on_contract_signed (Subscription, phase, native status) would silently never run.

The token is the same HMAC erpnext_mz uses for QR validation and ai_saas for
the Multipay pages: HMAC-SHA256(encryption_key, "Contract|<name>")[:16].
"""

import re

import frappe
from erpnext_mz.qr_code.qr_generator import _generate_validation_hash, validate_document_hash
from frappe.utils import get_url, now_datetime

from ai_saas.saas.lifecycle_mail import send_lifecycle_email

DOCTYPE = "Contract"


# ---------------------------------------------------------------------------
# token + url (the helper C2 / D3 render with)
# ---------------------------------------------------------------------------

def get_activation_token(contract_name: str) -> str:
	return _generate_validation_hash(DOCTYPE, contract_name)


def get_activation_url(contract_name: str) -> str:
	"""Full URL of the activation page for a contract — exposed to Jinja (hooks.jinja)."""
	return get_url(f"/activar?contract={contract_name}&token={get_activation_token(contract_name)}")


def get_reactivation_url(contract_name: str) -> str:
	"""The page where a suspended or closed account asks to come back (G2/G3) — same
	token as activation, so any email we ever sent still opens it."""
	return get_url(f"/reactivar?contract={contract_name}&token={get_activation_token(contract_name)}")


def is_valid_token(contract_name: str, token: str) -> bool:
	return bool(contract_name) and validate_document_hash(DOCTYPE, contract_name, token or "")


def cloud_plans():
	"""The plans self-service offers: Subscription Plans flagged mz_cloud_plan (seeded by
	install.ensure_cloud_plan_flags for the existing 'MozEconomia Cloud' plans)."""
	return frappe.get_all(
		"Subscription Plan",
		filters={"mz_cloud_plan": 1},
		fields=["name", "plan_name", "cost", "currency", "billing_interval", "billing_interval_count"],
		order_by="cost asc",
	)


# ---------------------------------------------------------------------------
# what the page shows
# ---------------------------------------------------------------------------

def get_activation_context(contract_name: str, token: str) -> dict:
	"""Everything /activar renders. Validates the token BEFORE reading anything."""
	ctx = frappe._dict(valid=False, already_signed=False, contract=None)
	if not is_valid_token(contract_name, token):
		return ctx
	if not frappe.db.exists(DOCTYPE, contract_name):
		return ctx

	c = frappe.get_doc(DOCTYPE, contract_name)
	if c.docstatus != 1 or c.party_type != "Customer":
		return ctx

	ctx.valid = True
	ctx.contract = c
	ctx.token = token
	ctx.already_signed = bool(c.is_signed)
	from ai_saas.saas.tenant_lifecycle import account_phase

	ctx.phase = account_phase(c.name)
	ctx.plans = cloud_plans()
	ctx.customer = frappe.db.get_value(
		"Customer", c.party_name, ["customer_name", "tax_id", "email_id", "mobile_no"], as_dict=True
	) or frappe._dict(customer_name=c.party_name)
	ctx.address = _get_billing_address(c.party_name) or frappe._dict()
	ctx.contact_email = c.get("contact_email") or ctx.customer.get("email_id") or ""
	ctx.terms = c.contract_terms
	ctx.trial_end = c.start_date
	ctx.site_url = f"https://{c.get('mz_tenant_url')}" if c.get("mz_tenant_url") else ""
	return ctx


def _get_billing_address(customer_name: str):
	links = frappe.get_all(
		"Dynamic Link",
		filters={"link_doctype": "Customer", "link_name": customer_name, "parenttype": "Address"},
		pluck="parent",
	)
	if not links:
		return None
	fields = ["name", "address_line1", "address_line2", "city", "state", "country", "address_type"]
	billing = frappe.db.get_value(
		"Address", {"name": ("in", links), "address_type": "Billing"}, fields, as_dict=True
	)
	return billing or frappe.db.get_value("Address", {"name": ("in", links)}, fields, as_dict=True)


# ---------------------------------------------------------------------------
# the transaction
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def activate(contract, token, plan=None, tax_id=None, address_line1=None, address_line2=None,
             city=None, contact_phone=None, accept_terms=0):
	"""Guest endpoint behind the page's confirm button. Thin wrapper: rate-limited
	per contract at the whitelist layer; the work is in _activate so tests call it
	without a request context."""
	from ai_saas.api.signup import _limit

	_limit(limit=10, seconds=60)                         # per IP
	_limit(identity=f"contract:{contract}", limit=10, seconds=60)
	return _activate(
		contract, token, plan=plan, tax_id=tax_id, address_line1=address_line1,
		address_line2=address_line2, city=city, contact_phone=contact_phone,
		accept_terms=frappe.utils.cint(accept_terms),
	)


def _activate(contract_name, token, plan=None, tax_id=None, address_line1=None,
              address_line2=None, city=None, contact_phone=None, accept_terms=0):
	if not is_valid_token(contract_name, token):
		frappe.throw("Ligação de activação inválida ou expirada.", frappe.PermissionError)
	if not accept_terms:
		frappe.throw("É necessário aceitar os termos do contrato para activar a conta.")

	c = frappe.get_doc(DOCTYPE, contract_name)
	if c.docstatus != 1 or c.party_type != "Customer":
		frappe.throw("Ligação de activação inválida ou expirada.", frappe.PermissionError)
	if c.is_signed:
		return {"already_signed": True, "site_url": f"https://{c.get('mz_tenant_url')}" if c.get("mz_tenant_url") else ""}
	# Never sign — and start billing — a contract with no usable site: archived,
	# never provisioned, or still failing. Old activation links outlive all of those.
	prov_status = frappe.db.get_value("MZ Tenant Provisioning", {"contract": c.name}, "status")
	if prov_status not in ("Active", "Suspended"):
		frappe.throw(
			"Esta conta já não pode ser activada por esta ligação. "
			"Contacte-nos em cloud@mozeconomia.co.mz e repomos o acesso."
		)

	# Corrections the customer is allowed to make — all on the Customer/Address,
	# except the plan, which is the one contract field editable after submit (B3).
	if plan and plan != c.get("mz_subscription_plan"):
		if c.get("mz_linked_subscription"):
			frappe.throw("O plano já não pode ser alterado — existe uma subscrição ligada.")
		if not frappe.db.exists("Subscription Plan", plan):
			frappe.throw("Plano inválido.")
		c.mz_subscription_plan = plan
	if tax_id is not None and tax_id.strip():
		from ai_saas.api.signup import NUIT_RE

		nuit = re.sub(r"\D", "", tax_id)
		if not NUIT_RE.match(nuit):
			frappe.throw("O NUIT tem 9 dígitos.")
		frappe.db.set_value("Customer", c.party_name, "tax_id", nuit)
	_upsert_billing_address(c.party_name, address_line1, address_line2, city)
	if contact_phone and contact_phone.strip():
		# The Contact is the source of the customer's phone; Customer.mobile_no and the
		# Contract's mirror follow it through ERPNext's own fetch on save.
		_set_primary_mobile(c.party_name, contact_phone.strip())

	was_suspended = prov_status == "Suspended"

	# The signature — through the save path, so B1's on_contract_signed fires:
	# Subscription with E2's billing start, phase Active, native status recompute.
	c.is_signed = 1
	c.signed_on = now_datetime()
	# The person who signs the relationship emails from here on — one setting, copied
	# once so the name does not change under the customer if the default does.
	if not c.get("mz_account_manager"):
		c.mz_account_manager = frappe.db.get_single_value("MZ SaaS Settings", "default_sales_user") or None
	c.signee = frappe.db.get_value("Customer", c.party_name, "customer_name") or c.party_name
	c.flags.ignore_permissions = True
	c.save(ignore_permissions=True)

	# Billing must never start against a site answering 503: old activation links
	# outlive a suspension, so reactivate as part of the signature itself.
	if was_suspended:
		from ai_saas.saas.tenant_lifecycle import reactivate

		try:
			reactivate(contract_name, notify=False)  # "Conta Activada" below says it all
		except Exception:
			# The signature is committed; the customer must not see an error for a
			# site the team can lift by hand. Record it and shout.
			frappe.log_error(title=f"AI SaaS: reactivate after signature failed ({contract_name})",
			                 message=frappe.get_traceback())
			from ai_saas.saas.alerts import notify_ops

			notify_ops(f"Reactivação falhou após assinatura: {contract_name}",
			           "<p>O contrato foi assinado mas o site continua em manutenção. Reactivar à mão.</p>")

	frappe.db.commit()
	send_lifecycle_email("activated", contract_name)
	billing_start = frappe.db.get_value(DOCTYPE, contract_name, "mz_billing_start")
	return {
		"ok": True,
		"site_url": f"https://{c.get('mz_tenant_url')}" if c.get("mz_tenant_url") else "",
		"billing_start": str(billing_start or ""),
		# Written the way the rest of the page writes dates — the raw value stays above
		# for any caller that needs to parse it.
		"billing_start_display": frappe.utils.formatdate(billing_start) if billing_start else "",
	}


def _set_primary_mobile(customer_name, mobile):
	contact = frappe.db.get_value("Customer", customer_name, "customer_primary_contact")
	if contact:
		doc = frappe.get_doc("Contact", contact)
		for row in doc.phone_nos:
			row.is_primary_mobile_no = 0
		doc.append("phone_nos", {"phone": mobile, "is_primary_mobile_no": 1})
		doc.flags.ignore_permissions = True
		doc.save()
	frappe.db.set_value("Customer", customer_name, "mobile_no", mobile)


def _upsert_billing_address(customer_name, line1, line2, city):
	from ai_saas.saas.mz_address import CITY_PROVINCE, canonical_city, parse_mz_address
	from ai_saas.saas.party import set_customer_primaries

	if not (line1 or line2 or city):
		return
	city = (city or "").strip()
	if city:
		city = canonical_city(city) or city
	elif line1:
		# The customer typed everything on one line: take the city out of it rather
		# than dropping an address that would otherwise be unsavable (city is reqd).
		city = parse_mz_address(line1)["city"]
	existing = _get_billing_address(customer_name)
	if existing:
		updates = {}
		if line1:
			updates["address_line1"] = line1.strip()
		if line2 is not None:
			updates["address_line2"] = (line2 or "").strip()
		if city:
			updates["city"] = city
			if not existing.state and CITY_PROVINCE.get(city):
				updates["state"] = CITY_PROVINCE[city]
		if existing.address_type != "Billing":
			updates["address_type"] = "Billing"
		if updates:
			frappe.db.set_value("Address", existing.name, updates)
		return
	if not (line1 and city):
		return  # a new address needs at least a line and a city
	title = frappe.db.get_value("Customer", customer_name, "customer_name") or customer_name
	address = frappe.get_doc({
		"doctype": "Address",
		"address_title": title,
		"address_type": "Billing",
		"address_line1": line1.strip(),
		"address_line2": (line2 or "").strip(),
		"city": city,
		"state": CITY_PROVINCE.get(city, ""),
		"country": "Mozambique",
		"is_primary_address": 1,
		"is_shipping_address": 1,
		"links": [{"link_doctype": "Customer", "link_name": customer_name}],
	})
	address.insert(ignore_permissions=True)
	# The trial had no usable address; this is the customer's first one, so it becomes
	# the primary — the address the first invoice will carry.
	set_customer_primaries(customer_name, address=address.name)
