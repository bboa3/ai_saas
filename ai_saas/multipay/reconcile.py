"""Background job: create and submit a Payment Entry for a paid ERPNext Payment Request."""

import frappe


def reconcile_payment(payment_request_name: str) -> None:
	"""Called by RQ after a successful SISLOG webhook.

	Delegates Payment Entry creation to ERPNext's native pr.create_payment_entry(),
	then stamps the Multipay custom fields. Idempotent: re-runs safely if already reconciled.
	"""
	frappe.set_user("Administrator")
	pr = frappe.get_doc("Payment Request", payment_request_name)

	# Native status is authoritative — covers both our flow and manual payments
	if pr.status == "Paid" or pr.mpay_status == "Paid":
		if pr.mpay_status != "Paid":
			frappe.db.set_value("Payment Request", payment_request_name, "mpay_status", "Paid")
			frappe.db.commit()
		return

	invoice = frappe.get_doc("Sales Invoice", pr.reference_name)

	if (invoice.outstanding_amount or 0) <= 0:
		frappe.db.set_value("Payment Request", payment_request_name, "mpay_status", "Paid")
		frappe.db.commit()
		return

	# Guard against crash-after-PE-creation: if a PE already references this PR
	# (create_payment_entry sets reference_no = pr.name), pick it up rather than creating another.
	existing_pe = frappe.db.get_value(
		"Payment Entry",
		{"reference_no": payment_request_name, "docstatus": ["!=", 2]},
		"name",
	)
	if existing_pe:
		frappe.db.set_value(
			"Payment Request",
			payment_request_name,
			{"mpay_status": "Paid", "mpay_payment_entry": existing_pe},
		)
		frappe.db.commit()
		return

	# Draft PRs skip before_submit() which normally sets outstanding_amount.
	# Both PE validate and the PE submit hook query this from the DB, so persist it first.
	frappe.db.set_value("Payment Request", payment_request_name, "outstanding_amount", pr.grand_total)
	pr.outstanding_amount = pr.grand_total

	try:
		pe = pr.create_payment_entry()
	except Exception:
		# If the PE was created but the exception occurred during submit or after,
		# the next reconcile run will find it via the existing_pe guard above.
		# Revert the dirty outstanding_amount so the daily task can retry cleanly.
		frappe.db.set_value("Payment Request", payment_request_name, "outstanding_amount", 0)
		frappe.log_error(
			title="Multipay: reconcile_payment failed",
			message=frappe.get_traceback(),
		)
		raise

	frappe.db.set_value(
		"Payment Request",
		payment_request_name,
		{
			"mpay_status": "Paid",
			"mpay_payment_entry": pe.name,
		},
	)
	frappe.db.set_value("Sales Invoice", pr.reference_name, "mz_latest_multipay_request", payment_request_name)
	frappe.db.commit()
