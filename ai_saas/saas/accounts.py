"""The account factory (workstream B, 2026-08-31): everything that turns a validated
MZ Signup into the funnel's documents — Lead/Opportunity (via saas.crm), Customer,
primary Contact, Billing Address, submitted Contract — plus the field validation the
web form and the desk share, and the desk entry point for direct sales.

api/signup.py keeps only the guest HTTP surface (wrappers, rate limits, token
lifecycle) and imports from here; nothing here imports api/signup.py.
"""

import re

import frappe
from frappe.utils import add_days, cint, now_datetime, nowdate, validate_email_address

from ai_saas.saas import crm
from ai_saas.saas.alerts import notify_ops
from ai_saas.saas.mz_address import CITY_PROVINCE
from ai_saas.saas.party import set_customer_primaries
from ai_saas.saas.provisioning import domain_for, domain_profile
from ai_saas.saas.settings import CONTRACT_TEMPLATE_TITLE as _CONTRACT_TEMPLATE_TITLE
from ai_saas.saas.settings import TRIAL_CUSTOMER_GROUP, get_settings


def _alert_ops(subject, message):
	notify_ops(f"[MozEconomia Cloud] {subject}", message)


STEP_FIELDS = {
	1: ("full_name", "email", "phone", "plan"),
	2: ("company_name", "tax_id", "tax_regime", "industry", "address", "city"),
	3: ("subdomain", "plan", "terms_accepted"),
}

NUIT_RE = re.compile(r"^\d{9}$")

def _validate_step(step, values):
	if step == 1 and "email" in values:
		email = (values["email"] or "").strip().lower()
		if not validate_email_address(email):
			frappe.throw("Indique um email válido.")
		values["email"] = email
	if step == 2:
		if "company_name" in values and not (values["company_name"] or "").strip():
			frappe.throw("Indique o nome da empresa.")
		if "tax_id" in values:
			nuit = re.sub(r"\D", "", values["tax_id"] or "")
			if not NUIT_RE.match(nuit):
				frappe.throw("O NUIT tem 9 dígitos.")
			values["tax_id"] = nuit
		if values.get("industry") and not frappe.db.exists("Segment Intelligence Map", values["industry"]):
			frappe.throw("Sector inválido.")
		if "address" in values:
			if len((values["address"] or "").strip()) < 5:
				frappe.throw("Indique o endereço da empresa (rua, bairro, cidade).")
			values["address"] = values["address"].strip()
	if step == 3:
		if "subdomain" in values:
			check = _check_subdomain(values["subdomain"])
			if not check["available"]:
				frappe.throw(check["reason"])
			values["subdomain"] = check["subdomain"]
		if values.get("plan") and not frappe.db.exists("Subscription Plan", values["plan"]):
			frappe.throw("Plano inválido.")
		if "terms_accepted" in values:
			values["terms_accepted"] = cint(values["terms_accepted"])

# Legal forms and filler words that never belong in a subdomain.
_LEGAL_FORMS = {
	"lda", "ltda", "limitada", "sa", "s.a", "sarl", "ei", "unipessoal", "sociedade", "soc", "cia", "companhia",
	"empresa", "grupo", "holding", "mocambique", "mozambique", "moçambique", "mz",
}

_STOPWORDS = {"de", "da", "do", "das", "dos", "e", "&", "a", "o", "as", "os", "em", "para", "por", "com"}

def slug_from_company(company_name: str, max_words: int = 3) -> str:
	"""A subdomain people would actually type: 'Farmácia Central, Lda' -> 'farmacia-central',
	'João & Filhos Comércio Geral Limitada' -> 'joao-filhos-comercio'. Diacritics stripped,
	legal forms and connectives dropped, first `max_words` meaningful words, slug rules applied."""
	import unicodedata

	text = unicodedata.normalize("NFKD", company_name or "").encode("ascii", "ignore").decode().lower()
	words = [w for w in re.split(r"[^a-z0-9]+", text) if w]
	words = [w for w in words if w not in _LEGAL_FORMS and w not in _STOPWORDS and not (len(w) == 1 and w.isalpha())]
	slug = "-".join(words[:max_words])[:40].strip("-")
	if len(slug) < 3 and words:
		slug = "-".join(words)[:40].strip("-")
	return slug

def _suggest_subdomain(company_name):
	"""The first free variant of the company's natural slug: name, name-2, name-3…"""
	base = slug_from_company(company_name)
	if len(base) < 3:
		return {"subdomain": ""}
	for candidate in [base] + [f"{base}-{n}" for n in range(2, 20)]:
		if _check_subdomain(candidate)["available"]:
			return {"subdomain": candidate}
	return {"subdomain": ""}

def _check_subdomain(subdomain):
	from ai_saas.saas.provisioning import validate_slug

	slug = (subdomain or "").strip().lower()
	try:
		validate_slug(slug)
	except frappe.ValidationError as exc:
		return {"available": False, "subdomain": slug, "reason": str(exc)}
	taken = (
		frappe.db.exists("Contract", {"mz_tenant": slug, "docstatus": ("!=", 2)})
		or frappe.db.exists("MZ Tenant Provisioning", {"tenant_slug": slug})
		or frappe.db.exists("MZ Signup", {"subdomain": slug, "status": ("in", ["Submitted", "Provisioning", "Complete"])})
	)
	if taken:
		return {"available": False, "subdomain": slug, "reason": "Este endereço já está ocupado. Escolha outro."}
	return {"available": True, "subdomain": slug, "reason": ""}

def _resolve_city(typed, address):
	"""The city for the Billing Address: what the visitor answered when we had to ask,
	otherwise whatever the one-line address gives up. A town we do not list is taken as
	typed — our list is a convenience, not a gate."""
	from ai_saas.saas.mz_address import canonical_city, parse_mz_address

	typed = (typed or "").strip()
	if typed:
		return canonical_city(typed) or typed
	return parse_mz_address(address or "")["city"]

def _find_duplicate(doc):
	"""What must never exist twice is a **cloud account for the same company**, and the
	company is its NUIT.

	An email that already belongs to a Customer is *not* a duplicate. This is
	MozEconomia's own ERP: it holds every customer the business has ever had — on-prem
	licences, consulting, POS — and one person can own or represent more than one
	company. Refusing on `Customer.email_id` (the rule until 2026-08-25) shut the door
	on exactly the people most likely to buy; A4 now reuses that Customer instead.
	"""
	twin = frappe.db.sql(
		"""SELECT k.name FROM `tabContract` k INNER JOIN `tabCustomer` c ON c.name = k.party_name
		   WHERE k.docstatus = 1 AND IFNULL(k.mz_tenant, '') <> '' AND c.tax_id = %s LIMIT 1""",
		doc.tax_id,
	)
	if twin:
		return f"O NUIT {doc.tax_id} já tem uma conta cloud ({twin[0][0]})"
	other = frappe.db.get_value(
		"MZ Signup",
		{"name": ("!=", doc.name), "status": ("in", ["Submitted", "Provisioning", "Complete"]),
		 "tax_id": doc.tax_id},
		"name",
	)
	if other:
		return f"Outro registo com o NUIT {doc.tax_id} já foi submetido: {other}"
	return None

def _existing_contact(customer_name, email):
	"""A Contact of this customer that already carries this email, if there is one."""
	rows = frappe.get_all("Contact Email", filters={"email_id": email}, pluck="parent")
	if not rows:
		return None
	return frappe.db.get_value(
		"Dynamic Link",
		{"parenttype": "Contact", "parent": ("in", rows), "link_doctype": "Customer", "link_name": customer_name},
		"parent",
	)

def _default_territory():
	"""Selling Settings default, else the root — translated on pt-MZ sites, so never assume its name."""
	return (
		frappe.db.get_single_value("Selling Settings", "territory")
		or frappe.db.get_value("Territory", {"is_group": 1, "parent_territory": ("in", ["", None])}, "name")
		or frappe.db.get_value("Territory", {}, "name", order_by="lft asc")
	)

def _create_documents(signup):
	"""A4: Lead, Opportunity, Customer (trial group), Contact, Billing Address,
	and the unsigned Contract — submitted, which provisions (B1)."""
	from erpnext.crm.doctype.contract_template.contract_template import get_contract_template

	from ai_saas.saas.provisioning import apps_for_segment
	s = get_settings()

	# Lead + Opportunity — entered at step 1 (_enter_crm); a signup flagged as a
	# duplicate of an account skipped that, so they are created here instead.
	if not signup.opportunity:
		crm.enter_crm(signup, stage=crm.STAGE_ACCOUNT_CREATED)
	lead_name = signup.lead
	crm.sync_crm(signup)

	# Customer — the company is its NUIT. An existing customer of the house (on-prem,
	# consulting, POS) buying the cloud product is the same customer, not a second one:
	# reuse the record, keep its commercial group, and never overwrite what sales set.
	existing = frappe.db.get_value("Customer", {"tax_id": signup.tax_id}, "name") if signup.tax_id else None
	if existing:
		customer = frappe.get_doc("Customer", existing)
		if not customer.lead_name:
			frappe.db.set_value("Customer", customer.name, "lead_name", lead_name)
	else:
		customer = frappe.get_doc({
			"doctype": "Customer", "customer_name": signup.company_name, "customer_type": "Company",
			"customer_group": TRIAL_CUSTOMER_GROUP,   # so sales reporting can tell trials apart
			"territory": _default_territory(),
			"tax_id": signup.tax_id, "lead_name": lead_name, "mobile_no": signup.phone,
		})
		customer.insert(ignore_permissions=True)

	# Contact (primary) — the chain Customer → primary contact → email_id every invoice
	# email needs. Reused when this person is already a contact of this customer.
	contact_name = _existing_contact(customer.name, signup.email)
	if not contact_name:
		contact = frappe.get_doc({
			"doctype": "Contact", "first_name": signup.full_name, "is_primary_contact": 1,
			"email_ids": [{"email_id": signup.email, "is_primary": 1}],
			"phone_nos": [{"phone": signup.phone, "is_primary_mobile_no": 1}] if signup.phone else [],
			"links": [{"link_doctype": "Customer", "link_name": customer.name}],
		})
		contact.insert(ignore_permissions=True)
		contact_name = contact.name
	elif not frappe.db.get_value("Contact", contact_name, "is_primary_contact"):
		frappe.db.set_value("Contact", contact_name, "is_primary_contact", 1)

	# Billing Address — parsed from the one-line address the lead typed (mz_address);
	# marked primary and shipping, as ERPNext's own make_address marks a first address.
	# The customer can still correct the lines at activation (E1).
	from ai_saas.saas.mz_address import parse_mz_address

	address_name = None
	parts = parse_mz_address(signup.address or signup.city or "")
	line1 = parts["address_line1"] or parts["address_line2"] or signup.city
	city = signup.city or parts["city"]
	if line1 and city:
		address = frappe.get_doc({
			"doctype": "Address", "address_title": signup.company_name, "address_type": "Billing",
			"address_line1": line1, "address_line2": parts["address_line2"] if parts["address_line2"] != line1 else "",
			"city": city, "state": parts["state"] or CITY_PROVINCE.get(city, ""), "country": "Mozambique",
			"is_primary_address": 1, "is_shipping_address": 1,
			"links": [{"link_doctype": "Customer", "link_name": customer.name}],
		})
		address.insert(ignore_permissions=True)
		address_name = address.name
	# No usable address (both fields are mandatory on Address): the account is still
	# created — E1 collects the billing address at activation, before the first invoice.

	# The Customer's own pointers, so it leaves the funnel addressable: primary contact,
	# primary address, email and mobile. Nothing sales already filled in is overwritten.
	# The Contract's contact fields are fetched from the Customer — nothing is copied.
	set_customer_primaries(customer, contact=contact_name, address=address_name,
	                       email=signup.email, mobile=signup.phone)
	frappe.db.set_value("Opportunity", signup.opportunity, "contact_person", contact_name)

	# Contract — unsigned, submitted: the trial begins (B1)
	slug = signup.subdomain
	domain = domain_for(signup.mz_domain)
	contract_fields = {
		"doctype": "Contract", "party_type": "Customer", "party_name": customer.name,
		"start_date": add_days(nowdate(), cint(signup.get("trial_days")) or s.trial_length_days),
		"contract_template": _CONTRACT_TEMPLATE_TITLE, "mz_direct": cint(signup.get("venda_directa")),
		"mz_subscription_plan": signup.plan, "mz_tenant": slug, "mz_domain": domain, "mz_tenant_url": slug + domain,
		"mz_segment": signup.industry,
		"mz_apps_to_install": [{"app_name": a} for a in apps_for_segment(signup.industry, signup.plan, domain)],
	}
	rendered = get_contract_template(_CONTRACT_TEMPLATE_TITLE, contract_fields)
	contract_fields["contract_terms"] = (
		rendered["contract_terms"] if isinstance(rendered, dict) else rendered.contract_terms
	)
	contract = frappe.get_doc(contract_fields)
	contract.insert(ignore_permissions=True)
	# Link the documents BEFORE submit: provisioning commits inside on_submit, so a
	# failure after that point must still leave the signup pointing at what exists.
	signup.db_set({"lead": lead_name, "customer": customer.name, "contract": contract.name}, update_modified=False)
	contract.submit()
	crm.report(signup.opportunity, crm.STAGE_ACCOUNT_CREATED)

	prov = frappe.db.get_value("MZ Tenant Provisioning", {"contract": contract.name}, "name")
	if not prov:
		# provisioning was swallowed (contract_lifecycle logs it): never leave the page
		# polling forever — fail loudly and tell the team.
		signup.update({"provisioning": None, "status": "Failed",
		               "error": "Contrato submetido mas nenhum registo de provisionamento foi criado."})
		signup.save(ignore_permissions=True)
		frappe.db.commit()
		_alert_ops(f"Sem provisionamento após submissão: {signup.email}",
		           f"MZ Signup {signup.name}, Contract {contract.name}: ver Error Log.")
		return
	signup.update({"provisioning": prov, "status": "Provisioning"})
	signup.save(ignore_permissions=True)
	frappe.db.commit()

@frappe.whitelist(methods=["POST"])
def create_account_from_desk(signup):
	"""Direct sales (decision 2026-08-31): Sales fills the same MZ Signup in the desk —
	usually with Venda Directa and a negotiation-sized trial window — and this creates
	the account through the exact pipeline /registo uses: same validations, same
	documents, same provisioning and delivery email. Never a guest endpoint."""
	frappe.only_for(("System Manager", "Sales Manager"))
	doc = frappe.get_doc("MZ Signup", signup)
	# Same retry rule as the web path: a Failed run that created no Contract may try again.
	if doc.status == "Failed" and not doc.contract:
		doc.status = "Started"
	if doc.status != "Started":
		frappe.throw(f"Este registo está em '{doc.status}' — só um registo em curso pode criar a conta.")

	labels = {"full_name": "nome do responsável", "email": "email", "plan": "plano",
	          "company_name": "nome da empresa", "tax_id": "NUIT", "address": "endereço", "subdomain": "subdomínio"}
	missing = [label for field, label in labels.items() if not str(doc.get(field) or "").strip()]
	if missing:
		frappe.throw("Preencha antes de criar a conta: " + ", ".join(missing) + ".")

	# The exact validations the web form enforces, step by step; normalised values kept.
	for step, fieldnames in STEP_FIELDS.items():
		values = {f: doc.get(f) for f in fieldnames}
		values["terms_accepted"] = 1  # o contrato é assinado fora do sistema
		_validate_step(step, values)
		doc.update(values)
	doc.city = _resolve_city(doc.city, doc.address)
	if not doc.city:
		frappe.throw("Indique a cidade — não foi possível lê-la do endereço.")

	duplicate = _find_duplicate(doc)
	if duplicate:
		frappe.throw(duplicate + ".")

	# One click creates one account. Take the row lock and flip the status before any
	# document exists: a second click — even from another tab, waiting on this lock —
	# finds the record no longer Started and is refused above on its own run.
	if frappe.db.get_value("MZ Signup", doc.name, "status", for_update=True) not in ("Started", "Failed"):
		frappe.throw("Este registo já está a criar a conta.")
	doc.status = "Submitted"
	doc.submitted_on = now_datetime()
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	try:
		_create_documents(doc)
	except Exception:
		frappe.db.rollback()
		doc.reload()
		doc.status = "Failed"
		doc.error = frappe.get_traceback()[-2000:]
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		frappe.log_error(title=f"AI SaaS: venda directa {doc.name} failed", message=frappe.get_traceback())
		raise
	return {"status": doc.status, "contract": doc.contract, "provisioning": doc.provisioning,
	        "opportunity": doc.opportunity}
