# Multipay (SISLOG) on MozEconomia Cloud

How a customer pays an invoice with M-Pesa or E-Mola, what the code does, and
what an operator watches. Written 2026-10-25 after the Multipay review; this
module is the **reference implementation** of the SISLOG wire shape (it was
sandbox-tested), and `curati_hub/multipay` mirrors it. Change the wire shape
here first.

## Wire shape (spec P17023X — MPAG@SISLOG REST API Reference, SISLOG_20240803)

| Call | Request | Reply |
|---|---|---|
| `POST {base_url}/mobile/reference/request` | header `apikey`; body `{"username","transactionId","value","cel"?}` | `{"status":"valid","entity","reference"}` or `{"status":…, "errorMessage"}` |
| `POST {base_url}/mobile/status/get` | header `apikey`; body `{"username","dateStart","dateEnd"}` (`yyyymmddHHMMSS`) | `{"status":"valid","entries":[{"transactionId","value",…}]}` |
| Callback (SISLOG → us) | `GET /api/method/ai_saas.multipay.api.payment_callback?token=<callback_token>&entity=…&reference=…&value=…&transactionId=…&provider=…&paymentdatetime=…` | always `200 {"status":"ok"}` |

`value` is the amount in centavos as an integer string (`50.00 MZN` → `5000`),
computed through `Decimal`. `transactionId` is 22 random alphanumeric
characters (never the Payment Request name). A failed payment arrives with
`entity=00000`, `value=0` and `errormessage`.

## Flow

1. The customer opens the invoice link (`/qr_validation?…&hash=<token>`; the
   token is the invoice's QR validation hash) and chooses M-Pesa or E-Mola
   with a phone number.
2. `initiate_payment` creates a native **Payment Request** (draft) against the
   Sales Invoice for its outstanding amount, asks SISLOG for an
   entity/reference (with a push to the phone), and stores them on the
   request. A second call while a request is Pending for the same invoice and
   method returns the same reference.
3. The page polls `get_payment_status` every 5 s for at most 10 minutes, then
   says "Ainda sem confirmação" and offers a retry.
4. SISLOG confirms through the callback (token and `value` mandatory) or the
   10-minute poll finds the transaction in `/mobile/status/get` (value
   checked). Either enqueues `reconcile_payment`, which books the Payment
   Entry (`Multipay Settings.payment_account`, the wallet's Mode of Payment)
   against the invoice.
5. A request Pending for 24 h becomes Expired (nightly); the next initiation
   makes a fresh reference. Tenant reactivation after payment stays a manual
   step (MZ Overdue Review).

## Settings (Multipay Settings)

`enabled`, `base_url`, `username`, `api_key`, `payment_account` (the Cúrati…
MozEconomia collection account the Payment Entry lands on), `callback_token`
(**required** for the webhook; `require_callback_token` is deprecated and
ignored), `absa_bank_account` (shown for bank transfers only).

## Operations

- Error Log titles: `Multipay callback: amount mismatch` (also an ops email),
  `Multipay callback: unknown transactionId` (at most once an hour),
  `Multipay: reconcile_payment failed`, `Multipay poll: amount mismatch`,
  `Multipay: request_reference HTTP error`, `Multipay: reference request failed`.
- A customer who says they paid but the invoice is still open: look at the
  Payment Request's "Resposta SISLOG (Raw)" and the SISLOG portal; if the
  request is Expired, book the Payment Entry by hand.
- Scheduled: `sync_pending_payments` every 10 minutes, `expire_pending_payments`
  daily.

## Tests

`ai_saas/tests/test_multipay.py` — run on a site with an open Sales Invoice:
`bench --site erp.local run-tests --skip-before-tests --app ai_saas --module ai_saas.tests.test_multipay`
(always `--skip-before-tests`: erpnext's before_tests deletes every Item Price).
