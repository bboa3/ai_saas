"""
House spelling (decision 2026-10-05): "fatura", "faturação" — never "factura".

The code and fixtures were respelled in the same commit; this brings along what a
site already holds in its database and no fixture overwrites:

- the Notification "AI SaaS - Trial - Primeira factura" is RENAMED, so the fixture
  sync that follows updates it instead of creating a second, duplicate sender;
- MZ Overdue Review.origin, whose stored option was "Facturação";
- the copy seeded create-if-missing: Email Templates, the Contract Template and
  the fiscal obligations (renamed too — they are named by their title). Respelled
  in place, so text the business edited is kept.

Signed contracts are left as they were signed.
"""

import re

import frappe

OLD_NOTIFICATION = "AI SaaS - Trial - Primeira factura"
NEW_NOTIFICATION = "AI SaaS - Trial - Primeira fatura"

_RULES = ((r"\bfactur", "fatur"), (r"\bFactur", "Fatur"), (r"\bFACTUR", "FATUR"))


def respell(text):
	for pattern, replacement in _RULES:
		text = re.sub(pattern, replacement, text)
	return text


def execute():
	if frappe.db.exists("Notification", OLD_NOTIFICATION):
		if frappe.db.exists("Notification", NEW_NOTIFICATION):
			frappe.delete_doc("Notification", OLD_NOTIFICATION, force=True, ignore_permissions=True)
		else:
			frappe.rename_doc("Notification", OLD_NOTIFICATION, NEW_NOTIFICATION, force=True)

	if frappe.db.has_column("MZ Overdue Review", "origin"):
		frappe.db.sql("UPDATE `tabMZ Overdue Review` SET origin = 'Faturação' WHERE origin = 'Facturação'")

	# Named by their title: rename first, or the seeder would add a respelled twin.
	if frappe.db.exists("DocType", "MZ Fiscal Obligation"):
		for name in frappe.get_all("MZ Fiscal Obligation", pluck="name"):
			if respell(name) != name and not frappe.db.exists("MZ Fiscal Obligation", respell(name)):
				frappe.rename_doc("MZ Fiscal Obligation", name, respell(name), force=True)

	targets = (
		("Email Template", {"name": ("like", "MozEconomia Cloud%")}, ("subject", "response", "response_html")),
		("Contract Template", {"name": "MozEconomia Cloud"}, ("contract_terms",)),
		("MZ Fiscal Obligation", {}, ("title", "who", "what_to_submit", "description")),
	)
	for doctype, filters, fields in targets:
		if not frappe.db.exists("DocType", doctype):
			continue
		fields = [f for f in fields if frappe.db.has_column(doctype, f)]
		for row in frappe.get_all(doctype, filters=filters, fields=["name", *fields]):
			changed = {f: respell(row[f]) for f in fields if row[f] and respell(row[f]) != row[f]}
			if changed:
				frappe.db.set_value(doctype, row.name, changed, update_modified=False)
