"""Scheduled tasks for Multipay — the polling safety net and the expiry."""

import frappe
from frappe.utils import add_to_date, now_datetime

from ai_saas.multipay.api import to_centavos

# A reference nobody pays: SISLOG references are short-lived and the status
# poll looks back one day, so after a day the request is dead either way.
PENDING_TTL_HOURS = 24


def sync_pending_payments() -> None:
	"""Every 10 minutes: poll SISLOG for Pending requests older than a minute.

	This is the fallback for a webhook that was never delivered (or never
	configured). Uses /mobile/status/get; the entry's value must match the
	request to the centavo, as the callback demands."""
	from ai_saas.multipay.client import get_sislog_status

	cutoff = add_to_date(now_datetime(), minutes=-1)
	pending = frappe.get_all(
		"Payment Request",
		filters={"mpay_status": "Pending", "creation": ["<", cutoff]},
		fields=["name", "mpay_transaction_id", "grand_total"],
	)
	if not pending:
		return

	date_start = add_to_date(now_datetime(), days=-1).strftime("%Y%m%d%H%M%S")
	date_end = now_datetime().strftime("%Y%m%d%H%M%S")
	paid_entries = get_sislog_status(date_start=date_start, date_end=date_end)
	paid_by_txn = {str(e.get("transactionId")): e for e in paid_entries if e.get("transactionId")}

	for pr in pending:
		entry = paid_by_txn.get(str(pr.mpay_transaction_id))
		if not entry:
			continue
		expected = to_centavos(pr.grand_total)
		try:
			paid = int(str(entry.get("value")).strip())
		except (TypeError, ValueError):
			paid = None
		if paid != expected:
			frappe.log_error(
				title="Multipay poll: amount mismatch",
				message=f"{pr.name}: SISLOG reports {entry.get('value')}, request is {expected}",
			)
			continue
		frappe.enqueue(
			"ai_saas.multipay.reconcile.reconcile_payment",
			payment_request_name=pr.name,
			queue="default",
			job_name=f"multipay_reconcile_{pr.name}",
			enqueue_after_commit=False,
		)


def expire_pending_payments() -> dict:
	"""Daily: a request Pending for more than PENDING_TTL_HOURS becomes Expired.
	initiate_payment then stops handing its dead entity/reference back and
	creates a fresh one; a later SISLOG success for the old id is ignored by
	the callback (it acts on Pending only) and the operator refunds by hand if
	a customer proves they paid."""
	cutoff = add_to_date(now_datetime(), hours=-PENDING_TTL_HOURS)
	stale = frappe.get_all(
		"Payment Request", filters={"mpay_status": "Pending", "creation": ["<", cutoff]}, pluck="name"
	)
	for name in stale:
		frappe.db.set_value("Payment Request", name, "mpay_status", "Expired")
	frappe.db.commit()
	return {"expired": stale}
