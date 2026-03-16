import frappe

TARGET_STAGE = "Cloud - Form Submitted"


def on_lead_onboarding_save(doc, method=None):
	"""
	Fires after Lead Onboarding is inserted or updated.
	1. Copies company data fields onto the linked Lead.
	2. Finds the most recent Opportunity for the Lead and sets sales_stage = TARGET_STAGE.
	"""
	if not doc.lead:
		frappe.throw("Lead Onboarding: campo 'lead' está vazio. Não sei qual Lead atualizar.")

	# --- 1. Update Lead fields ---
	lead_meta = frappe.get_meta("Lead")
	lead_updates = {}

	def add_if_exists(fieldname, value):
		if value in (None, ""):
			return
		if lead_meta.get_field(fieldname):
			lead_updates[fieldname] = value

	add_if_exists("company_name", doc.company_name)
	add_if_exists("email_id", doc.company_email)
	add_if_exists("mobile_no", doc.company_phone)
	add_if_exists("phone", doc.company_phone)
	add_if_exists("city", doc.company_address)

	if lead_updates:
		frappe.db.set_value("Lead", doc.lead, lead_updates, update_modified=False)

	# --- 2. Update Opportunity sales_stage ---
	opp_name = frappe.db.get_value(
		"Opportunity",
		{"opportunity_from": "Lead", "party_name": doc.lead},
		"name",
		order_by="creation desc",
	)

	if not opp_name:
		frappe.throw(f"Nenhuma Opportunity encontrada para o Lead {doc.lead}.")

	opp = frappe.get_doc("Opportunity", opp_name)
	opp.sales_stage = TARGET_STAGE
	opp.save(ignore_permissions=True)
	_create_account_setup_task(doc, opp_name)


def _create_account_setup_task(doc, opp_name):
	"""Create a ToDo for the sales team to set up the customer account and contract."""
	existing = frappe.db.get_value(
		"ToDo",
		{"reference_type": "Opportunity", "reference_name": opp_name, "status": "Open"},
		"name",
	)
	if existing:
		return

	company_name = doc.company_name or ""
	description = (
		f"<b>Lead qualificado — acção necessária</b><br><br>"
		f"A empresa <b>{company_name}</b> preencheu o formulário de activação.<br><br>"
		f"<b>Passos a seguir:</b><br>"
		f"<ol>"
		f"<li>Criar o Cliente no ERPNext</li>"
		f"<li>Criar e assinar o Contrato (plano + data de início)</li>"
		f"<li>O sistema cria a Subscrição automaticamente após assinatura</li>"
		f"</ol>"
		f"<br>Opportunity: {opp_name}"
	)

	frappe.get_doc({
		"doctype": "ToDo",
		"status": "Open",
		"priority": "High",
		"description": description,
		"reference_type": "Opportunity",
		"reference_name": opp_name,
		"assigned_by": frappe.session.user,
	}).insert(ignore_permissions=True)
