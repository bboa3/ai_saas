"""Remove the defunct Multipay Payment Request custom DocType.

This DocType was an early prototype replaced by extending the native ERPNext
Payment Request with custom fields (mpay_* prefix). Two stale records existed
in tabMultipay Payment Request at the time this patch was written; both are
safe to delete (test/development data, never used in production).

Idempotent: the outer guard means re-running after a successful migration is a
no-op.
"""

import frappe


def execute():
	"""Drop all records and the DocType definition for Multipay Payment Request."""
	if not frappe.db.exists("DocType", "Multipay Payment Request"):
		return

	# Delete data rows before removing the DocType so FK constraints (if any) do
	# not block the operation, and so the table is empty before Frappe attempts
	# to drop it via delete_doc.
	frappe.db.delete("Multipay Payment Request")
	frappe.db.commit()

	frappe.delete_doc("DocType", "Multipay Payment Request", force=True, ignore_missing=True)
	frappe.db.commit()
