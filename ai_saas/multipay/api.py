"""Public API endpoints and webhook handler for Multipay.

All guest-facing methods validate the HMAC token before touching the database.

Webhook (Option 2 — preferred):
  SISLOG sends a GET request to the configured callback URL.
  URL must be registered in your SISLOG account as:
      https://<your-site>/api/method/ai_saas.multipay.api.payment_callback?token=<callback_token>

  GET params: entity, reference, value, transactionId, provider, paymentdatetime
  Failed payment marker: entity == "00000"

The callback token is mandatory (2026-10-25): with no token configured no
callback is accepted and the 10-minute poll (tasks.sync_pending_payments)
reconciles alone. This module is the reference implementation of the SISLOG
wire shape; curati_hub/multipay mirrors it.
"""

import hmac as _hmac
import json
from decimal import ROUND_HALF_UP, Decimal

import frappe
from erpnext_mz.qr_code.qr_generator import validate_document_hash as _validate_token
from frappe import _

GATEWAY_ERROR_MESSAGE = (
	"Não foi possível contactar o serviço de pagamentos. Por favor tente novamente dentro de instantes."
)
RECONCILE_DEDUP_SECONDS = 60
UNKNOWN_ID_LOG_SECONDS = 3600


def to_centavos(amount) -> int:
	"""MZN -> integer centavos without binary-float rounding surprises."""
	return int(Decimal(str(amount or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)


def _new_transaction_id() -> str:
	"""SISLOG: alphanumeric, max 22. Random, not the Payment Request name with
	the hyphens removed: that was sequential, and the callback below is a guest
	endpoint keyed on it."""
	return frappe.generate_hash(length=22)


def _validate_invoice_token(invoice_name: str, token: str) -> bool:
	return _validate_token("Sales Invoice", invoice_name, token)


# ---------------------------------------------------------------------------
# Public guest endpoints
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
def get_payment_page_data(invoice_name: str, token: str) -> dict:
	"""Return invoice summary for the public payment page.

	Token is validated before any DB access.
	"""
	from ai_saas.api.signup import _limit

	_limit(limit=60, seconds=60)
	if not _validate_invoice_token(invoice_name, token):
		frappe.throw(_("Link de pagamento inválido ou expirado."), frappe.PermissionError)

	inv = frappe.db.get_value(
		"Sales Invoice",
		invoice_name,
		[
			"name",
			"customer",
			"customer_name",
			"grand_total",
			"outstanding_amount",
			"due_date",
			"currency",
			"status",
			"mz_latest_multipay_request",
		],
		as_dict=True,
	)
	if not inv:
		frappe.throw(_("Fatura não encontrada."), frappe.DoesNotExistError)

	return {
		"invoice_name": inv.name,
		"customer_name": inv.customer_name,
		"grand_total": inv.grand_total,
		"outstanding_amount": inv.outstanding_amount,
		"due_date": str(inv.due_date) if inv.due_date else None,
		"currency": inv.currency,
		"already_paid": (inv.outstanding_amount or 0) <= 0,
		"latest_multipay_request": inv.mz_latest_multipay_request,
	}


@frappe.whitelist(allow_guest=True)
def initiate_payment(invoice_name: str, token: str, method: str, phone_number: str = "") -> dict:
	"""Create a Payment Request, call SISLOG, return entity + reference.

	Idempotent: if a Pending request already exists for this invoice + method,
	return it instead of creating a new one (even if SISLOG hasn't responded yet).
	"""
	from ai_saas.api.signup import _limit

	_limit(limit=10, seconds=60)
	_limit(identity=f"invoice:{invoice_name}", limit=10, seconds=60)
	from ai_saas.multipay.client import request_reference

	if not _validate_invoice_token(invoice_name, token):
		frappe.throw(_("Link de pagamento inválido ou expirado."), frappe.PermissionError)

	if method not in ("E-Mola", "M-Pesa"):
		frappe.throw(_("Canal de pagamento inválido. Use E-Mola ou M-Pesa."), frappe.ValidationError)

	inv = frappe.db.get_value(
		"Sales Invoice",
		invoice_name,
		["name", "outstanding_amount", "grand_total", "currency", "customer", "docstatus"],
		as_dict=True,
	)
	if not inv:
		frappe.throw(_("Fatura não encontrada."), frappe.DoesNotExistError)

	if inv.docstatus != 1:
		frappe.throw(_("Esta fatura não está confirmada."), frappe.ValidationError)

	if (inv.outstanding_amount or 0) <= 0:
		frappe.throw(_("Esta fatura já se encontra paga."), frappe.ValidationError)

	# Idempotency: return any existing Pending request for same invoice + method,
	# including those still waiting for a SISLOG response (mpay_entity may be empty).
	existing = frappe.db.get_value(
		"Payment Request",
		{"reference_name": invoice_name, "mpay_status": "Pending", "mpay_method": method},
		["name", "mpay_transaction_id", "mpay_entity", "mpay_reference"],
		as_dict=True,
	)
	if existing:
		return {
			"transaction_id": existing.mpay_transaction_id,
			"entity": existing.mpay_entity,
			"reference": existing.mpay_reference,
			"payment_request": existing.name,
		}

	settings = frappe.get_single("Multipay Settings")
	if not settings.payment_account:
		frappe.throw(
			_("Conta de pagamento Multipay não configurada. Contacte o administrador."),
			frappe.ValidationError,
		)

	# Create new Payment Request — name auto-generated (ACC-PRQ-YYYY-XXXXX)
	# payment_account is required by create_payment_entry() as the "paid_to" bank account.
	pr = frappe.get_doc(
		{
			"doctype": "Payment Request",
			"payment_request_type": "Inward",
			"reference_doctype": "Sales Invoice",
			"reference_name": invoice_name,
			"grand_total": inv.outstanding_amount,
			"currency": inv.currency,
			"party_type": "Customer",
			"party": inv.customer,
			"mode_of_payment": method,
			"payment_account": settings.payment_account,
			"mpay_method": method,
			"mpay_status": "Pending",
		}
	)
	pr.flags.ignore_permissions = True
	pr.insert()

	transaction_id = _new_transaction_id()
	frappe.db.set_value("Payment Request", pr.name, "mpay_transaction_id", transaction_id)
	frappe.db.commit()

	# Call SISLOG. The client logs the detail; the customer gets one fixed
	# sentence, never the exception text or SISLOG's own wording.
	try:
		cel = phone_number.strip() or None
		sislog_resp = request_reference(transaction_id, inv.outstanding_amount, cel=cel)
	except Exception:
		frappe.db.set_value("Payment Request", pr.name, "mpay_status", "Failed")
		frappe.db.commit()
		frappe.throw(_(GATEWAY_ERROR_MESSAGE), frappe.ValidationError)

	frappe.db.set_value(
		"Payment Request",
		pr.name,
		{
			"mpay_entity": sislog_resp.get("entity"),
			"mpay_reference": sislog_resp.get("reference"),
			"mpay_sislog_raw": json.dumps(sislog_resp),
		},
	)
	frappe.db.set_value("Sales Invoice", invoice_name, "mz_latest_multipay_request", pr.name)
	frappe.db.commit()

	return {
		"entity": sislog_resp.get("entity"),
		"reference": sislog_resp.get("reference"),
		"payment_request": pr.name,
		"transaction_id": transaction_id,
	}


@frappe.whitelist(allow_guest=True)
def get_payment_status(payment_request: str, token: str) -> dict:
	"""Return the current Multipay status of a Payment Request (used for polling).

	Requires the invoice token to prevent unauthenticated enumeration of PR data.
	"""
	from ai_saas.api.signup import _limit

	_limit(limit=120, seconds=60)
	pr = frappe.db.get_value(
		"Payment Request",
		payment_request,
		[
			"name",
			"mpay_status",
			"mpay_entity",
			"mpay_reference",
			"mpay_payment_entry",
			"mpay_transaction_id",
			"reference_name",
		],
		as_dict=True,
	)
	# One answer for "no such request" and "not your request": a bare request
	# name must not reveal whether it exists.
	if not pr or not _validate_invoice_token(pr.reference_name, token):
		frappe.throw(_("Link de pagamento inválido ou expirado."), frappe.PermissionError)

	return {
		"payment_request": pr.name,
		"status": pr.mpay_status,
		"entity": pr.mpay_entity,
		"reference": pr.mpay_reference,
		"payment_entry": pr.mpay_payment_entry,
		"transaction_id": pr.mpay_transaction_id,
	}


# ---------------------------------------------------------------------------
# SISLOG webhook receiver (Option 2)
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
def payment_callback() -> dict:
	"""SISLOG sends a GET request with payment confirmation params.

	URL registered with SISLOG:
	  https://<site>/api/method/ai_saas.multipay.api.payment_callback?token=<callback_token>

	GET params (success): entity, reference, value, transactionId, provider, paymentdatetime
	GET params (failure):  entity=00000, reference=00000000000, value=0,
	                       transactionId, provider, errormessage, paymentdatetime
	"""
	args = frappe.request.args

	# The shared token travels in the registered URL's query string. Mandatory,
	# constant-time, fail closed: no token configured means no callback is
	# accepted (the poll reconciles alone). Always 200 - never reveal token
	# validity to probers. `require_callback_token` is no longer consulted.
	try:
		stored_token = (
			frappe.get_single("Multipay Settings").get_password("callback_token", raise_exception=False) or ""
		)
	except Exception:
		stored_token = ""
	provided_token = args.get("token") or ""
	if not stored_token or not provided_token or not _hmac.compare_digest(provided_token, stored_token):
		return {"status": "ok"}

	transaction_id = args.get("transactionId") or ""
	entity = args.get("entity") or ""

	if not transaction_id:
		return {"status": "ok"}

	# Failed payment marker: entity == "00000"
	is_failure = entity == "00000"

	pr_name = frappe.db.get_value("Payment Request", {"mpay_transaction_id": transaction_id}, "name")
	if not pr_name:
		# One log line an hour, not one per probe: this is a guest endpoint.
		cache = frappe.cache()
		if cache.set(cache.make_key("mpay_unknown_txn_logged"), 1, nx=True, ex=UNKNOWN_ID_LOG_SECONDS):
			frappe.log_error(
				title="Multipay callback: unknown transactionId",
				message=f"transactionId={transaction_id} entity={entity} (further unknown ids are not logged for an hour)",
			)
		return {"status": "ok"}

	current_status = frappe.db.get_value("Payment Request", pr_name, "mpay_status")
	if current_status != "Pending":
		# Already processed — idempotent 200
		return {"status": "ok"}

	if is_failure:
		frappe.db.set_value(
			"Payment Request",
			pr_name,
			{
				"mpay_status": "Failed",
				"mpay_sislog_raw": json.dumps(
					{
						"failed": True,
						"errormessage": args.get("errormessage") or "Payment failed",
						"provider": args.get("provider"),
						"paymentdatetime": args.get("paymentdatetime"),
						"transactionId": transaction_id,
						"entity": entity,
					}
				),
			},
		)
		frappe.db.commit()
		return {"status": "ok"}

	# The SISLOG-reported amount is mandatory and must match to the centavo.
	reported_value = args.get("value")
	if reported_value in (None, ""):
		return {"status": "ok"}
	pr_grand_total = frappe.db.get_value("Payment Request", pr_name, "grand_total") or 0
	expected = to_centavos(pr_grand_total)
	try:
		if int(str(reported_value).strip()) != expected:
			frappe.log_error(
				title="Multipay callback: amount mismatch",
				message=f"transactionId={transaction_id} expected={expected} got={reported_value}",
			)
			_notify_ops_safely(
				"Multipay: valor divergente",
				f"<p>O callback SISLOG para o pedido <b>{pr_name}</b> reporta <b>{reported_value}</b> centavos; "
				f"o pedido é de <b>{expected}</b>. Não foi reconciliado — verifique o pagamento manualmente.</p>",
				pr_name,
			)
			return {"status": "ok"}
	except (ValueError, TypeError):
		frappe.log_error(
			title="Multipay callback: invalid value field",
			message=f"transactionId={transaction_id} value={reported_value}",
		)
		return {"status": "ok"}

	# Success — enqueue reconciliation once per minute per request (SISLOG
	# retries its notification; job_name is not a dedup key in Frappe v15).
	cache = frappe.cache()
	if not cache.set(cache.make_key(f"mpay_reconcile:{pr_name}"), 1, nx=True, ex=RECONCILE_DEDUP_SECONDS):
		return {"status": "ok"}
	frappe.enqueue(
		"ai_saas.multipay.reconcile.reconcile_payment",
		payment_request_name=pr_name,
		queue="default",
		job_name=f"multipay_reconcile_{pr_name}",
		enqueue_after_commit=False,
	)

	return {"status": "ok"}


def _notify_ops_safely(subject: str, html: str, payment_request: str | None = None) -> None:
	try:
		from ai_saas.saas.alerts import notify_ops

		notify_ops(subject, html, reference_doctype="Payment Request", reference_name=payment_request)
	except Exception:
		frappe.log_error(title="Multipay: ops alert failed", message=frappe.get_traceback())
