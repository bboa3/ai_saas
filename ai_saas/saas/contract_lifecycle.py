import frappe


def on_contract_signed(doc, method=None):
	"""Create Subscription when a contract is signed — fires on both on_submit and on_update_after_submit."""
	if doc.party_type != "Customer":
		return
	if not doc.is_signed:
		return
	if not doc.get("mz_subscription_plan"):
		return
	if doc.get("mz_linked_subscription"):
		return  # already created, nothing to do
	_setup_subscription(doc)


def on_contract_cancel(doc, method=None):
	"""Cancel the linked Subscription when the contract is cancelled."""
	sub_name = doc.get("mz_linked_subscription")
	if not sub_name or not frappe.db.exists("Subscription", sub_name):
		return
	try:
		sub = frappe.get_doc("Subscription", sub_name)
		if sub.status != "Cancelled":
			sub.cancel_subscription()
			sub.save(ignore_permissions=True)
			frappe.db.commit()
	except Exception:
		frappe.log_error(
			title=f"AI SaaS: Failed to cancel Subscription {sub_name}",
			message=frappe.get_traceback(),
		)



def _setup_subscription(doc):
	"""Create an ERPNext Subscription from the contract's selected Subscription Plan."""
	plan_name = doc.mz_subscription_plan
	if not frappe.db.exists("Subscription Plan", plan_name):
		frappe.log_error(
			title=f"AI SaaS: Subscription Plan '{plan_name}' not found",
			message=f"Contract: {doc.name}",
		)
		return

	company = _get_company()
	if not company:
		frappe.log_error(
			title="AI SaaS: No default company found",
			message=f"Contract: {doc.name} — Set a default company in Global Defaults.",
		)
		return

	sub = frappe.get_doc({
		"doctype": "Subscription",
		"party_type": "Customer",
		"party": doc.party_name,
		"company": company,
		"start_date": doc.start_date,
		"end_date": doc.end_date or None,
		"generate_invoice_at": "Beginning of the current subscription period",
		"submit_invoice": 1,
		"days_until_due": 7,
		"generate_new_invoices_past_due_date": 0,
		"sales_tax_template": _get_default_tax_template(company),
		"plans": [{"plan": plan_name, "qty": 1}],
	})
	# Subscription is not submittable — insert() is sufficient.
	# The controller auto-sets status="Active" during validate.
	sub.insert(ignore_permissions=True)
	frappe.db.commit()

	frappe.db.set_value("Contract", doc.name, "mz_linked_subscription", sub.name)
	_ensure_customer_primary_contact(doc)
	frappe.db.commit()


def _ensure_customer_primary_contact(doc):
	"""Set customer_primary_contact on the Customer from the Contract's contact_person.

	ERPNext Customer.email_id is fetched from customer_primary_contact.email_id.
	Without a primary contact, Customer.email_id stays NULL and no notification
	can reach the customer via Sales Invoice.contact_email (which fetches from customer.email_id).
	"""
	contact_person = doc.get("contact_person")
	if not contact_person:
		return
	customer_name = doc.party_name
	existing = frappe.db.get_value("Customer", customer_name, "customer_primary_contact")
	if existing:
		return  # already set, do not overwrite
	frappe.db.set_value("Customer", customer_name, "customer_primary_contact", contact_person)
	# Trigger the fetch so customer.email_id is populated immediately
	contact_email = frappe.db.get_value("Contact", contact_person, "email_id")
	if contact_email:
		frappe.db.set_value("Customer", customer_name, "email_id", contact_email)


def _get_company():
	"""Resolve the default company for subscription/invoice generation."""
	return (
		frappe.defaults.get_user_default("company")
		or frappe.db.get_single_value("Global Defaults", "default_company")
	)


def _get_default_tax_template(company):
	"""Return the default Sales Taxes and Charges Template for the company, or None."""
	return frappe.db.get_value(
		"Sales Taxes and Charges Template",
		{"company": company, "is_default": 1},
		"name",
	)
