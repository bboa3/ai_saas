import frappe


def set_contact_email(doc, method=None):
	"""Populate contact_email on Payment Entry before submit so the receipt notification has a recipient."""
	if doc.contact_email or doc.party_type != "Customer" or not doc.party:
		return

	# Find the primary contact linked to this customer
	contact = frappe.db.get_value(
		"Dynamic Link",
		{"link_doctype": "Customer", "link_name": doc.party, "parenttype": "Contact"},
		"parent",
		order_by="creation asc",
	)
	if not contact:
		return

	# Prefer the primary email; fall back to the first email on that contact
	email = frappe.db.get_value(
		"Contact Email",
		{"parent": contact, "is_primary": 1},
		"email_id",
	) or frappe.db.get_value(
		"Contact Email",
		{"parent": contact},
		"email_id",
		order_by="idx asc",
	)

	if email:
		doc.contact_email = email
