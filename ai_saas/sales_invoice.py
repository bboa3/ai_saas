import frappe


def set_contact_mobile(doc, method=None):
	"""Populate contact_mobile on Sales Invoice before submit so the SMS notification has a recipient.

	ERPNext copies contact_mobile from Contact.mobile_no only (erpnext/accounts/party.py), and
	Contact.validate() blanks mobile_no unless a phone_nos row has is_primary_mobile_no=1. A contact
	whose number is flagged only as is_primary_phone therefore produces an empty contact_mobile and
	the notification is skipped in silence. Resolve the number ourselves so the flag stops mattering.
	"""
	if doc.contact_mobile or not doc.customer:
		return

	contact = doc.contact_person or frappe.db.get_value(
		"Dynamic Link",
		{"link_doctype": "Customer", "link_name": doc.customer, "parenttype": "Contact"},
		"parent",
		order_by="creation asc",
	)
	if not contact:
		return

	# Prefer the primary mobile, then the primary phone, then whatever number is on the contact
	phone = (
		frappe.db.get_value("Contact Phone", {"parent": contact, "is_primary_mobile_no": 1}, "phone")
		or frappe.db.get_value("Contact Phone", {"parent": contact, "is_primary_phone": 1}, "phone")
		or frappe.db.get_value("Contact Phone", {"parent": contact}, "phone", order_by="idx asc")
	)

	if phone:
		doc.contact_mobile = phone
