"""The Customer's own pointers: primary contact and primary address.

ERPNext reads these wherever the customer is addressed — `Customer.email_id` is fetched
from the primary contact and `Sales Invoice.contact_email` from that, print formats and
party details read `customer_primary_address`. A Customer the funnel creates must
therefore leave with both set; a Customer that already existed keeps whatever sales put
there, so these helpers only ever fill a blank.
"""

import frappe


def set_customer_primaries(customer, contact=None, address=None, email=None, mobile=None):
	"""Fill the Customer's empty primary fields. `customer` is a name or a doc.

	Returns what was written, so callers can tell filled from left-alone.
	"""
	from frappe.contacts.doctype.address.address import get_address_display

	name = customer if isinstance(customer, str) else customer.name
	current = frappe.db.get_value(
		"Customer", name,
		["customer_primary_contact", "customer_primary_address", "email_id", "mobile_no"],
		as_dict=True,
	) or frappe._dict()

	updates = {}
	if contact and not current.customer_primary_contact:
		updates["customer_primary_contact"] = contact
	if address and not current.customer_primary_address:
		updates["customer_primary_address"] = address
		updates["primary_address"] = get_address_display(address)   # the read-only display ERPNext prints
	if not current.email_id:
		email = email or (contact and frappe.db.get_value("Contact", contact, "email_id"))
		if email:
			updates["email_id"] = email
	if not current.mobile_no:
		mobile = mobile or (contact and frappe.db.get_value("Contact", contact, "mobile_no"))
		if mobile:
			updates["mobile_no"] = mobile

	if updates:
		frappe.db.set_value("Customer", name, updates)
		if not isinstance(customer, str):
			customer.reload()
	return updates


def ensure_customer_primaries(customer_name):
	"""Safety net for customers the funnel did not create — the desk path, or data from
	before A4 existed: resolve the contact and the billing address through Dynamic Link.
	"""
	contact = _best_linked(customer_name, "Contact", "is_primary_contact")
	address = _best_linked(customer_name, "Address", "is_primary_address", prefer={"address_type": "Billing"})
	return set_customer_primaries(customer_name, contact=contact, address=address)


def _best_linked(customer_name, doctype, primary_field, prefer=None):
	"""The linked document that most deserves to be the primary one: the one already
	flagged primary, then the preferred kind (a Billing address), then anything linked."""
	names = frappe.get_all(
		"Dynamic Link",
		filters={"link_doctype": "Customer", "link_name": customer_name, "parenttype": doctype},
		pluck="parent",
	)
	if not names:
		return None
	for filters in [{primary_field: 1}] + ([prefer] if prefer else []):
		hit = frappe.db.get_value(doctype, dict(filters, name=("in", names)), "name")
		if hit:
			return hit
	return names[0]
