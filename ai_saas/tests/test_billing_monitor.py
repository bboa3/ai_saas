"""Tests for billing_monitor (F1/F5): the daily dunning job must at least run,
and its review/follow-up helpers must dedup. Invoice rows are passed as dicts —
the helpers only need the fields the SQL returns."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

from ai_saas.saas import billing_monitor

TEST_CUSTOMER = "_Test Cliente AI SaaS BM"
TEST_PLAN = "Premium Mensal - MozEconomia Cloud"
FAKE_SUB = "SUB-TEST-BM-0001"


class TestBillingMonitor(FrappeTestCase):
	def setUp(self):
		from ai_saas.tests.helpers import ensure_test_plan
		ensure_test_plan()
		if not frappe.db.exists("Customer", TEST_CUSTOMER):
			frappe.get_doc({
				"doctype": "Customer", "customer_name": TEST_CUSTOMER, "customer_type": "Company",
				"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
				"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
			}).insert(ignore_permissions=True)
		with patch("ai_saas.saas.provisioning.provision_tenant"):
			self.contract = frappe.get_doc({
				"doctype": "Contract", "party_type": "Customer", "party_name": TEST_CUSTOMER,
				"start_date": nowdate(), "contract_terms": "Termos BM.", "mz_subscription_plan": TEST_PLAN,
				"contact_email": "bm@example.com",
			}).insert(ignore_permissions=True)
			self.contract.submit()
		frappe.db.set_value("Contract", self.contract.name, "mz_linked_subscription", FAKE_SUB)
		frappe.db.commit()

	def tearDown(self):
		for dt, flt in (("MZ Overdue Review", {"contract": self.contract.name}),
		                ("ToDo", {"reference_type": "Contract", "reference_name": self.contract.name})):
			for n in frappe.get_all(dt, flt, pluck="name"):
				frappe.delete_doc(dt, n, force=True, ignore_permissions=True)
		# Event.on_trash syncs user settings through the cache Redis; a plain row delete
		# keeps this test runnable with the bench stopped.
		for n in frappe.get_all("Event", {"subject": ("like", f"%{TEST_CUSTOMER}%")}, pluck="name"):
			frappe.db.delete("Event Participants", {"parent": n})
			frappe.db.delete("Event", {"name": n})
		# The fake subscription link would fail link validation on cancel — clear it first.
		frappe.db.set_value("Contract", self.contract.name, "mz_linked_subscription", None, update_modified=False)
		doc = frappe.get_doc("Contract", self.contract.name)
		if doc.docstatus == 1:
			doc.cancel()
		frappe.delete_doc("Contract", doc.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Customer", TEST_CUSTOMER, force=True, ignore_missing=True, ignore_permissions=True)
		frappe.db.commit()

	def _invoice(self, days_overdue):
		return frappe._dict(name="SINV-TEST-BM", customer=TEST_CUSTOMER, subscription=FAKE_SUB,
		                    outstanding_amount=2999.0, due_date=add_days(nowdate(), -days_overdue),
		                    days_overdue=days_overdue)

	def test_daily_job_runs(self):
		"""The blocker: a bad column in the pre-billing query killed the whole job every day."""
		with patch("frappe.sendmail"):
			billing_monitor._send_prebilling_reminders()
			billing_monitor._get_overdue_invoices()

	def test_prebilling_email_prices_cost_times_qty(self):
		"""Per-user pricing: the 'Valor Estimado' is plan cost x the plan row's qty."""
		from ai_saas.saas.contract_lifecycle import _get_company

		lead_days = billing_monitor.get_settings().prebilling_reminder_days
		sub = frappe.get_doc({
			"doctype": "Subscription", "party_type": "Customer", "party": TEST_CUSTOMER,
			"company": _get_company(), "start_date": nowdate(),
			"generate_invoice_at": "Beginning of the current subscription period",
			"plans": [{"plan": TEST_PLAN, "qty": 5}],
		}).insert(ignore_permissions=True)
		try:
			frappe.db.set_value("Subscription", sub.name, "current_invoice_start", add_days(nowdate(), lead_days))
			# The rolled-back series recycles subscription names, so a stale lab contract
			# may already point at this name — the reverse lookup must find ours. The
			# update is uncommitted and dies with the test transaction.
			frappe.db.sql(
				"UPDATE tabContract SET mz_linked_subscription = NULL "
				"WHERE mz_linked_subscription = %s AND name != %s",
				(sub.name, self.contract.name),
			)
			# contact_email is fetched from the Customer's primary contact on insert (there
			# is none in this suite) — set it directly, the reminder refuses to send without it.
			frappe.db.set_value("Contract", self.contract.name,
			                    {"mz_linked_subscription": sub.name, "contact_email": "bm@example.com"},
			                    update_modified=False)
			with patch("frappe.sendmail") as sendmail:
				billing_monitor._send_prebilling_reminders()
			sent = [c for c in sendmail.call_args_list
			        if c.kwargs.get("reference_name") == self.contract.name]
			self.assertEqual(len(sent), 1)
			message = sent[0].kwargs["message"]
			# qty 5 with no seats on the contract -> 6 seats shown (billed + the included first).
			self.assertIn("6 utilizadores (1.º incluído): 5", message)
			from frappe.utils.formatters import format_value

			total = format_value(5 * 2999, {"fieldtype": "Currency", "currency": "MZN"})
			self.assertIn(total, message)
		finally:
			frappe.db.set_value("Contract", self.contract.name, "mz_linked_subscription", FAKE_SUB,
			                    update_modified=False)
			frappe.delete_doc("Subscription", sub.name, force=True, ignore_permissions=True)

	def test_overdue_review_is_created_once_per_invoice(self):
		inv = self._invoice(1)
		billing_monitor._create_overdue_reviews([inv])
		billing_monitor._create_overdue_reviews([inv])
		reviews = frappe.get_all("MZ Overdue Review", {"contract": self.contract.name}, ["review_status", "overdue_since"])
		self.assertEqual(len(reviews), 1)
		self.assertEqual(reviews[0].review_status, "Pending Review")
		# The team acting on it must not make the daily job re-create it.
		frappe.db.set_value("MZ Overdue Review", {"contract": self.contract.name}, "review_status", "Suspend")
		billing_monitor._create_overdue_reviews([inv])
		self.assertEqual(frappe.db.count("MZ Overdue Review", {"contract": self.contract.name}), 1)

	def test_followup_respects_threshold_and_dedups(self):
		real = frappe._dict(overdue_followup_days=7)
		with patch("ai_saas.saas.billing_monitor.get_settings", return_value=real):
			billing_monitor._create_followup_tasks([self._invoice(6)])
			self.assertEqual(frappe.db.count("ToDo", {"reference_type": "Contract", "reference_name": self.contract.name}), 0)
			billing_monitor._create_followup_tasks([self._invoice(7)])
			billing_monitor._create_followup_tasks([self._invoice(8)])
		self.assertEqual(frappe.db.count("ToDo", {"reference_type": "Contract", "reference_name": self.contract.name, "status": "Open"}), 1)
