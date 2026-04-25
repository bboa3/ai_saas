"""Public payment page controller.

Route: /pay/<invoice_name>?token=<hmac_token>

The token is HMAC-SHA256("pay|<invoice_name>", encryption_key)[:16].
It is validated before any database read.
"""

import frappe

no_cache = 1
no_breadcrumbs = 1


def get_context(context):
	invoice_name = frappe.form_dict.get("invoice_name") or ""
	token = frappe.form_dict.get("token") or ""

	context.invoice_name = invoice_name
	context.token = token
	context.not_found = False
	context.invalid_token = False
	context.invoice = None

	if not invoice_name:
		context.not_found = True
		return

	from ai_saas.multipay.tokens import validate_payment_token

	if not validate_payment_token(invoice_name, token):
		context.invalid_token = True
		return

	inv = frappe.db.get_value(
		"Sales Invoice",
		invoice_name,
		[
			"name", "customer", "customer_name", "company",
			"posting_date", "due_date", "currency",
			"net_total", "total_taxes_and_charges", "grand_total", "outstanding_amount",
			"taxes_and_charges", "tax_category", "status",
			"mz_latest_multipay_request",
		],
		as_dict=True,
	)

	if not inv:
		context.not_found = True
		return

	# Fetch customer NUIT (tax_id) from the Customer record
	if inv.customer:
		inv.customer_tax_id = frappe.db.get_value("Customer", inv.customer, "tax_id") or ""
	else:
		inv.customer_tax_id = ""

	context.invoice = inv
	context.already_paid = (inv.outstanding_amount or 0) <= 0
	context.title = f"Pagamento — {invoice_name}"
