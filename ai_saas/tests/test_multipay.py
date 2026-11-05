"""Multipay (SISLOG) — the rail MozEconomia Cloud is paid through had no tests
(2026-10-25 review). These pin the fail-closed callback, the random
transaction id, the poll's amount check and the expiry. The gateway itself is
never called: request_reference / get_sislog_status are patched."""

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from erpnext_mz.qr_code.qr_generator import _generate_validation_hash
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime
from frappe.utils.password import remove_encrypted_password, set_encrypted_password

from ai_saas.multipay import api, tasks
from ai_saas.multipay.api import get_payment_status, initiate_payment, payment_callback, to_centavos

SETTINGS = "Multipay Settings"
TOKEN = "_test-mpay-callback-token-0123456789"


@contextmanager
def _request(args: dict):
	"""payment_callback reads frappe.request.args and the rate limiter wants a
	request object; the test process has none (frappe.local is a werkzeug
	Local, so patch.object cannot stand one in)."""
	frappe.local.request = SimpleNamespace(args=args, method="GET")
	try:
		yield
	finally:
		delattr(frappe.local, "request")


CUSTOMER = "_Test Multipay Customer"


def _open_invoice():
	"""A submitted invoice with an outstanding amount, inside the test
	transaction (commit is patched by every caller, so the rollback removes it)."""
	from ai_saas.tests.helpers import TEST_ITEM, ensure_test_plan, make_test_customer

	ensure_test_plan()
	customer = make_test_customer(CUSTOMER)
	company = frappe.defaults.get_global_default("company") or frappe.get_all("Company", pluck="name")[0]
	doc = frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"customer": customer,
			"company": company,
			"items": [{"item_code": TEST_ITEM, "qty": 1, "rate": 123.45}],
		}
	)
	doc.insert(ignore_permissions=True)
	doc.submit()
	return frappe._dict(
		name=doc.name, outstanding_amount=doc.outstanding_amount, currency=doc.currency, customer=customer
	)


class _InvoiceFixture(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		# The per-IP limiter keys on "no-ip" in a test process and would trip
		# across this module's initiations within one minute.
		cache = frappe.cache()
		for key in cache.keys("rl:signup:*"):
			cache.delete(key)
		try:
			with patch("frappe.db.commit"):
				self.invoice = _open_invoice()
		except Exception as exc:
			self.skipTest(f"could not create a test invoice here: {exc}")
		self.token = _generate_validation_hash("Sales Invoice", self.invoice.name)
		self.saved = frappe.db.get_singles_dict(SETTINGS)
		frappe.db.set_single_value(
			SETTINGS, {"enabled": 1, "base_url": "http://sislog.invalid", "username": "u"}
		)
		if not frappe.db.get_single_value(SETTINGS, "payment_account"):
			account = frappe.db.get_value(
				"Account", {"account_type": ["in", ("Bank", "Cash")], "is_group": 0}, "name"
			)
			frappe.db.set_single_value(SETTINGS, "payment_account", account)
		self.addCleanup(self._restore)

	def _restore(self):
		frappe.db.set_single_value(
			SETTINGS, {k: self.saved.get(k) for k in ("enabled", "base_url", "username", "payment_account")}
		)
		remove_encrypted_password(SETTINGS, SETTINGS, "callback_token")

	def _set_token(self, value):
		if value:
			set_encrypted_password(SETTINGS, SETTINGS, value, "callback_token")
		else:
			remove_encrypted_password(SETTINGS, SETTINGS, "callback_token")

	def _initiate(self, method="M-Pesa", phone="258841234567"):
		sislog = {"status": "valid", "entity": "11111", "reference": "123456789"}
		with (
			_request({}),
			patch("frappe.db.commit"),
			patch("ai_saas.multipay.client.request_reference", return_value=sislog) as rr,
		):
			reply = initiate_payment(self.invoice.name, self.token, method, phone)
		return reply, rr

	def _status(self, name):
		return frappe.db.get_value("Payment Request", name, "mpay_status")


class TestInitiate(_InvoiceFixture):
	def test_transaction_id_is_random_and_sislog_shaped(self):
		reply, rr = self._initiate()
		tid = reply["transaction_id"]
		self.assertEqual(len(tid), 22)
		self.assertTrue(tid.isalnum())
		self.assertNotEqual(tid, reply["payment_request"].replace("-", ""))
		rr.assert_called_once()
		self.assertEqual(rr.call_args.args[0], tid)
		self.assertEqual(reply["entity"], "11111")

	def test_second_call_returns_the_live_request(self):
		first, _ = self._initiate()
		second, rr = self._initiate()
		self.assertEqual(second["payment_request"], first["payment_request"])
		rr.assert_not_called()

	def test_gateway_failure_shows_one_fixed_sentence(self):
		with (
			_request({}),
			patch("frappe.db.commit"),
			patch("ai_saas.multipay.client.request_reference", side_effect=RuntimeError("boom: internal detail")),
		):
			with self.assertRaises(frappe.ValidationError) as caught:
				initiate_payment(self.invoice.name, self.token, "E-Mola", "258871234567")
		self.assertNotIn("internal detail", str(caught.exception))

	def test_status_needs_the_token_first(self):
		reply, _ = self._initiate()
		with _request({}):
			with self.assertRaises(frappe.PermissionError):
				get_payment_status(reply["payment_request"], "wrong")
			with self.assertRaises(frappe.PermissionError):
				get_payment_status("ACC-PRQ-nope", "wrong")  # same answer: nothing to enumerate
			self.assertEqual(get_payment_status(reply["payment_request"], self.token)["status"], "Pending")


class TestCallback(_InvoiceFixture):
	def setUp(self):
		super().setUp()
		reply, _ = self._initiate()
		self.pr = reply["payment_request"]
		self.tid = reply["transaction_id"]
		self.centavos = str(to_centavos(self.invoice.outstanding_amount))
		frappe.cache().delete_value(f"mpay_reconcile:{self.pr}")
		self.addCleanup(lambda: frappe.cache().delete_value(f"mpay_reconcile:{self.pr}"))

	def _callback(self, **kwargs):
		args = {
			"transactionId": self.tid,
			"entity": "11111",
			"reference": "123456789",
			"value": self.centavos,
			"token": TOKEN,
		}
		args.update(kwargs)
		args = {k: v for k, v in args.items() if v is not None}
		with _request(args), patch("frappe.enqueue") as enqueue, patch("frappe.db.commit"):
			reply = payment_callback()
		self.assertEqual(reply, {"status": "ok"})
		return enqueue

	def test_no_token_configured_means_no_callback(self):
		self._set_token(None)
		self._callback().assert_not_called()
		self.assertEqual(self._status(self.pr), "Pending")

	def test_wrong_or_missing_token_is_ignored(self):
		self._set_token(TOKEN)
		self._callback(token="guess").assert_not_called()
		self._callback(token=None).assert_not_called()
		self.assertEqual(self._status(self.pr), "Pending")

	def test_value_is_mandatory_and_must_match(self):
		self._set_token(TOKEN)
		with patch.object(api, "_notify_ops_safely"):
			self._callback(value=None).assert_not_called()
			self._callback(value=str(int(self.centavos) + 1)).assert_not_called()
			self._callback(value="abc").assert_not_called()
		self.assertEqual(self._status(self.pr), "Pending")

	def test_success_reconciles_once(self):
		self._set_token(TOKEN)
		enqueue = self._callback()
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["payment_request_name"], self.pr)
		self._callback().assert_not_called()  # SISLOG retry within a minute: no second job

	def test_failure_marker_closes_the_request_only_with_the_token(self):
		self._set_token(TOKEN)
		self._callback(
			entity="00000", reference="00000000000", value="0", errormessage="saldo"
		).assert_not_called()
		self.assertEqual(self._status(self.pr), "Failed")

	def test_unknown_transaction_is_ignored(self):
		self._set_token(TOKEN)
		self._callback(transactionId="nope").assert_not_called()


class TestPoll(_InvoiceFixture):
	def setUp(self):
		super().setUp()
		reply, _ = self._initiate()
		self.pr = reply["payment_request"]
		self.tid = reply["transaction_id"]
		self.centavos = str(to_centavos(self.invoice.outstanding_amount))
		frappe.db.set_value(
			"Payment Request",
			self.pr,
			"creation",
			add_to_date(now_datetime(), minutes=-5),
			update_modified=False,
		)

	def test_matching_entry_is_reconciled(self):
		entries = [
			{"transactionId": self.tid, "value": self.centavos},
			{"transactionId": "other", "value": "1"},
		]
		with (
			patch("ai_saas.multipay.client.get_sislog_status", return_value=entries),
			patch("frappe.enqueue") as enqueue,
		):
			tasks.sync_pending_payments()
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["payment_request_name"], self.pr)

	def test_wrong_value_is_not_reconciled(self):
		entries = [{"transactionId": self.tid, "value": str(int(self.centavos) - 1)}]
		with (
			patch("ai_saas.multipay.client.get_sislog_status", return_value=entries),
			patch("frappe.enqueue") as enqueue,
		):
			tasks.sync_pending_payments()
		enqueue.assert_not_called()

	def test_stale_request_expires(self):
		frappe.db.set_value(
			"Payment Request",
			self.pr,
			"creation",
			add_to_date(now_datetime(), hours=-(tasks.PENDING_TTL_HOURS + 1)),
			update_modified=False,
		)
		with patch("frappe.db.commit"):
			result = tasks.expire_pending_payments()
		self.assertIn(self.pr, result["expired"])
		self.assertEqual(self._status(self.pr), "Expired")
		# A new initiation no longer hands the dead reference back.
		reply, rr = self._initiate()
		self.assertNotEqual(reply["payment_request"], self.pr)
		rr.assert_called_once()


class TestCentavos(FrappeTestCase):
	def test_decimal_not_float(self):
		self.assertEqual(to_centavos(50), 5000)
		self.assertEqual(to_centavos(2.675), 268)
		self.assertEqual(to_centavos("0.29"), 29)
