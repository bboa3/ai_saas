"""Scheduled tasks for Multipay — daily polling safety net."""

import frappe
from frappe.utils import add_to_date, now_datetime


def sync_pending_payments() -> None:
	"""Daily job: poll SISLOG for any Pending requests older than 10 minutes.

	This is a fallback for cases where the webhook was never delivered.
	Uses /mobile/status/get (Option 1) — to be replaced once Option 2 is reliable.
	"""
	from ai_saas.multipay.client import get_sislog_status

	cutoff = add_to_date(now_datetime(), minutes=-10)
	pending = frappe.get_all(
		"Payment Request",
		filters={"mpay_status": "Pending", "creation": ["<", cutoff]},
		fields=["name", "mpay_transaction_id"],
	)

	if not pending:
		return

	# Fetch all recently paid entries from SISLOG (last 24h)
	# SISLOG format: yyyymmddHHMMSS  — use strftime directly for reliability
	date_start = add_to_date(now_datetime(), days=-1).strftime("%Y%m%d%H%M%S")
	date_end = now_datetime().strftime("%Y%m%d%H%M%S")
	paid_entries = get_sislog_status(date_start=date_start, date_end=date_end)

	paid_by_txn = {e.get("transactionId"): e for e in paid_entries if e.get("transactionId")}

	for pr in pending:
		if pr.mpay_transaction_id in paid_by_txn:
			frappe.enqueue(
				"ai_saas.multipay.reconcile.reconcile_payment",
				payment_request_name=pr.name,
				queue="default",
				job_name=f"multipay_reconcile_{pr.name}",
				enqueue_after_commit=False,
			)
