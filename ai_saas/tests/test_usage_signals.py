"""Tests for D1 (probe + snapshots) and D2 (hot/cold signals).
The bench subprocess is patched; no tenant site is ever touched."""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

from ai_saas.saas import crm, usage_signals
from ai_saas.saas.provisioning import ProvisioningError

TEST_CUSTOMER = "_Test Cliente AI SaaS D"
TEST_PLAN = "Premium Mensal - MozEconomia Cloud"
TEST_SLUG = "d1-teste"


class TestUsageSignals(FrappeTestCase):
	def setUp(self):
		from ai_saas.tests.helpers import ensure_test_plan
		ensure_test_plan()
		if not frappe.db.exists("Customer", TEST_CUSTOMER):
			frappe.get_doc({
				"doctype": "Customer",
				"customer_name": TEST_CUSTOMER,
				"customer_type": "Company",
				"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
				"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
			}).insert(ignore_permissions=True)
		with patch("ai_saas.saas.provisioning.provision_tenant"):
			self.contract = frappe.get_doc({
				"doctype": "Contract",
				"party_type": "Customer",
				"party_name": TEST_CUSTOMER,
				"start_date": add_days(nowdate(), 14),
				"contract_terms": "Termos de teste D.",
				"mz_subscription_plan": TEST_PLAN,
				"mz_tenant": TEST_SLUG,
			}).insert(ignore_permissions=True)
			self.contract.submit()
		self.prov = frappe.get_doc({
			"doctype": "MZ Tenant Provisioning",
			"contract": self.contract.name,
			"tenant_slug": TEST_SLUG,
			"site_name": f"{TEST_SLUG}.erp.mozeconomia.co.mz",
			"status": "Active",
		}).insert(ignore_permissions=True)
		self.opp = frappe.get_doc({
			"doctype": "Opportunity",
			"opportunity_from": "Customer",
			"party_name": TEST_CUSTOMER,
			"sales_stage": "Cloud - Account Created",
		}).insert(ignore_permissions=True)
		# Every sweep in these tests is scoped with contracts=[...]: an unscoped sweep would
		# feed the patched probe's numbers to the REAL trials on the site and move their
		# Opportunities. The creation-time cleanup below is the safety net.
		self._started_at = frappe.utils.now_datetime()
		frappe.db.commit()
		# Signal ToDos email their assignee — the REAL default sales user when the test
		# Opportunity has none. Mock it for the whole class; tests that assert on the
		# email re-patch the same attribute inside.
		self._mail = patch("ai_saas.saas.crm.frappe.sendmail")
		self._mail.start()

	def _drop_snapshots_written_by_this_test(self):
		for name in frappe.get_all(
			"MZ Tenant Usage Snapshot", filters={"creation": (">=", self._started_at)}, pluck="name"
		):
			frappe.delete_doc("MZ Tenant Usage Snapshot", name, force=True, ignore_permissions=True)

	def tearDown(self):
		self._mail.stop()
		self._drop_snapshots_written_by_this_test()
		for t in frappe.get_all("ToDo", {"reference_type": "Opportunity", "reference_name": self.opp.name}, pluck="name"):
			frappe.delete_doc("ToDo", t, force=True, ignore_permissions=True)
		for sn in frappe.get_all("MZ Tenant Usage Snapshot", {"contract": self.contract.name}, pluck="name"):
			frappe.delete_doc("MZ Tenant Usage Snapshot", sn, force=True, ignore_permissions=True)
		frappe.delete_doc("Opportunity", self.opp.name, force=True, ignore_missing=True, ignore_permissions=True)
		frappe.delete_doc("MZ Tenant Provisioning", self.prov.name, force=True, ignore_missing=True, ignore_permissions=True)
		doc = frappe.get_doc("Contract", self.contract.name)
		if doc.docstatus == 1:
			doc.cancel()
		frappe.delete_doc("Contract", doc.name, force=True, ignore_permissions=True)
		frappe.delete_doc("Customer", TEST_CUSTOMER, force=True, ignore_missing=True, ignore_permissions=True)
		frappe.db.commit()

	@staticmethod
	def _bench_output(payload):
		# bench execute JSON-encodes the function's return value, which is itself a JSON string.
		return json.dumps(json.dumps(payload)) + "\n"

	# ---- D1 ---------------------------------------------------------------------

	def test_probe_parses_bench_output_and_records_failure(self):
		payload = {"invoice_count": 3, "first_invoice_date": "2026-08-20", "user_count": 2, "last_login": "2026-08-23 10:00:00"}
		with patch("ai_saas.saas.usage_signals.run_cmd_capture", return_value=self._bench_output(payload)):
			snap = usage_signals._probe("x.erp.mozeconomia.co.mz")
		self.assertEqual(snap["probe_ok"], 1)
		self.assertEqual(snap["invoice_count"], 3)
		self.assertEqual(snap["first_invoice_date"], "2026-08-20")

		with patch("ai_saas.saas.usage_signals.run_cmd_capture", side_effect=ProvisioningError("site em baixo")):
			snap = usage_signals._probe("x.erp.mozeconomia.co.mz")
		self.assertEqual(snap["probe_ok"], 0)
		self.assertIn("site em baixo", snap["error"])

	def test_probe_runs_inside_the_tenant_and_stays_off_the_network(self):
		"""The probe lives in erpnext_mz — the app every tenant site has — and must never
		become an HTTP endpoint there: un-whitelisted, it is reachable only from a shell
		on the host. Adding @frappe.whitelist() to it fails this test."""
		from erpnext_mz.utils import tenant_usage

		self.assertEqual(usage_signals.PROBE_METHOD, "erpnext_mz.utils.tenant_usage.usage_snapshot")
		self.assertFalse(getattr(tenant_usage.usage_snapshot, "whitelisted", False))
		self.assertNotIn("erpnext_mz.utils.tenant_usage.usage_snapshot", frappe.whitelisted)

		# It reads counts, never content: the four indicators and nothing else.
		payload = json.loads(tenant_usage.usage_snapshot())
		self.assertEqual(set(payload), set(usage_signals.INT_FIELDS) | {"first_invoice_date", "last_login"})

	def test_collect_writes_one_snapshot_per_day(self):
		payload = {"invoice_count": 0, "first_invoice_date": None, "user_count": 1, "last_login": None}
		with patch("ai_saas.saas.usage_signals.run_cmd_capture", return_value=self._bench_output(payload)) as probe:
			usage_signals.collect_usage_snapshots(contracts=[self.contract.name])
			usage_signals.collect_usage_snapshots(contracts=[self.contract.name])  # same day: no second row, no second probe
		rows = frappe.get_all("MZ Tenant Usage Snapshot", {"contract": self.contract.name}, ["probe_ok", "user_count"])
		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0].probe_ok, 1)
		self.assertEqual(rows[0].user_count, 1)
		self.assertEqual(probe.call_count, 1)

	def test_collect_skips_suspended_site(self):
		frappe.db.set_value("MZ Tenant Provisioning", self.prov.name, "status", "Suspended")
		with patch("ai_saas.saas.usage_signals.run_cmd_capture") as probe:
			usage_signals.collect_usage_snapshots(contracts=[self.contract.name])
		probe.assert_not_called()

	# ---- D2 ---------------------------------------------------------------------

	def _row(self, **over):
		row = frappe._dict(name=self.contract.name, party_name=TEST_CUSTOMER,
		                   creation=self.contract.creation, start_date=self.contract.start_date)
		row.update(over)
		return row

	def _stage(self):
		return frappe.db.get_value("Opportunity", self.opp.name, "sales_stage")

	def _open_todos(self):
		return frappe.get_all("ToDo", {"reference_type": "Opportunity", "reference_name": self.opp.name, "status": "Open"},
		                      ["description", "priority"])

	def test_first_invoice_alone_is_not_a_signal(self):
		"""One invoice — often the one we created together on the activation call — scores nothing."""
		snap = frappe._dict(invoice_count=1, first_invoice_date=nowdate(), last_login=frappe.utils.now(),
		                    invoice_days_7d=1, invoice_days_prev_7d=0, active_users_7d=1, master_data_count=1, other_docs_30d=0)
		self.assertEqual(usage_signals.score(snap)[0], 0)
		self.assertEqual(usage_signals.evaluate_signals(self._row(), snap), "")
		self.assertEqual(self._stage(), "Cloud - Account Created")
		self.assertEqual(self._open_todos(), [])

	def test_engaged_needs_momentum_and_breadth_and_fires_once(self):
		snap = frappe._dict(invoice_count=6, first_invoice_date=nowdate(), last_login=frappe.utils.now(),
		                    invoice_days_7d=3, invoice_days_prev_7d=1, active_users_7d=1, master_data_count=12, other_docs_30d=0)
		points, reasons = usage_signals.score(snap)
		self.assertEqual(points, 4)  # 2 (3 days) + 1 (accelerating) + 1 (own master data)
		self.assertEqual(len(reasons), 3)

		self.assertEqual(usage_signals.evaluate_signals(self._row(), snap), "Engaged")
		usage_signals.evaluate_signals(self._row(), snap)
		self.assertEqual(self._stage(), usage_signals.STAGE_ENGAGED)
		todos = self._open_todos()
		self.assertEqual(len(todos), 1)
		self.assertTrue(todos[0].description.startswith(usage_signals.MARKER_HOT))
		self.assertIn("4 pontos", todos[0].description)
		self.assertEqual(todos[0].priority, "High")

	def test_cooling_needs_earlier_activity_then_silence(self):
		silent = frappe._dict(invoice_count=4, first_invoice_date=add_days(nowdate(), -20),
		                      last_login=add_days(nowdate(), -9) + " 10:00:00",
		                      invoice_days_7d=0, invoice_days_prev_7d=0, active_users_7d=0, master_data_count=3, other_docs_30d=0)
		# No earlier snapshot showing real work → not decay.
		self.assertEqual(usage_signals.evaluate_signals(self._row(), silent), "")

		# An earlier snapshot with only a login (looked around once) is still not "was active".
		frappe.get_doc({
			"doctype": "MZ Tenant Usage Snapshot", "contract": self.contract.name, "site_name": self.prov.site_name,
			"snapshot_date": add_days(nowdate(), -11), "probe_ok": 1, "invoice_count": 0,
			"last_login": add_days(nowdate(), -11) + " 09:00:00",
		}).insert(ignore_permissions=True)
		self.assertEqual(usage_signals.evaluate_signals(self._row(), silent), "")

		frappe.get_doc({
			"doctype": "MZ Tenant Usage Snapshot", "contract": self.contract.name, "site_name": self.prov.site_name,
			"snapshot_date": add_days(nowdate(), -10), "probe_ok": 1, "invoice_count": 4,
			"last_login": add_days(nowdate(), -10) + " 09:00:00",
		}).insert(ignore_permissions=True)
		self.assertEqual(usage_signals.evaluate_signals(self._row(), silent), "Cooling")
		self.assertEqual(self._stage(), usage_signals.STAGE_AT_RISK)
		todos = self._open_todos()
		self.assertEqual(len(todos), 1)
		self.assertTrue(todos[0].description.startswith(usage_signals.MARKER_COOLING))

		# Logged in 2 days ago → not silent.
		recent = frappe._dict(silent, last_login=add_days(nowdate(), -2) + " 10:00:00")
		self.assertFalse(usage_signals._went_silent(self._row(), recent))

	def test_cold_signal_past_midpoint_without_activity(self):
		# Logged in once, entered nothing, invoiced nothing: Cold, not Cooling.
		snap = frappe._dict(invoice_count=0, first_invoice_date=None, master_data_count=0,
		                    last_login=add_days(nowdate(), -9) + " 10:00:00")
		row = self._row(creation=add_days(nowdate(), -12), start_date=add_days(nowdate(), 2))
		self.assertEqual(usage_signals.evaluate_signals(row, snap), "Cold")
		self.assertEqual(self._stage(), usage_signals.STAGE_AT_RISK)

		# Before the midpoint nothing fires.
		frappe.db.set_value("Opportunity", self.opp.name, "sales_stage", "Cloud - Account Created")
		row = self._row(creation=add_days(nowdate(), -2), start_date=add_days(nowdate(), 12))
		self.assertEqual(usage_signals.evaluate_signals(row, snap), "")
		self.assertEqual(self._stage(), "Cloud - Account Created")

	def test_sweep_stores_score_and_signal_on_the_row(self):
		payload = {"invoice_count": 6, "first_invoice_date": nowdate(), "user_count": 2, "last_login": frappe.utils.now(),
		           "invoice_days_7d": 3, "invoice_days_prev_7d": 1, "invoice_count_30d": 6,
		           "active_users_7d": 2, "master_data_count": 12, "other_docs_30d": 1}
		with patch("ai_saas.saas.usage_signals.run_cmd_capture", return_value=self._bench_output(payload)):
			usage_signals.collect_usage_snapshots(contracts=[self.contract.name])
		row = frappe.get_all("MZ Tenant Usage Snapshot", {"contract": self.contract.name},
		                     ["engagement_score", "signal", "invoice_days_7d", "master_data_count"])[0]
		self.assertEqual((row.engagement_score, row.signal), (6, "Engaged"))
		self.assertEqual((row.invoice_days_7d, row.master_data_count), (3, 12))
		self.assertEqual(self._stage(), usage_signals.STAGE_ENGAGED)

	def test_signal_emails_the_assignee(self):
		"""A ToDo inserted from code notifies nobody, so the signal must also be mailed."""
		frappe.db.set_value("Opportunity", self.opp.name, "_assign", json.dumps(["Administrator"]))
		snap = frappe._dict(invoice_count=6, first_invoice_date=nowdate(), last_login=frappe.utils.now(),
		                    invoice_days_7d=3, invoice_days_prev_7d=1, active_users_7d=2, master_data_count=12, other_docs_30d=0)
		with patch("ai_saas.saas.crm.frappe.sendmail") as sendmail:
			usage_signals.evaluate_signals(self._row(), snap)
		sendmail.assert_called_once()
		kwargs = sendmail.call_args.kwargs
		self.assertEqual(kwargs["recipients"], [frappe.db.get_value("User", "Administrator", "email")])
		self.assertIn(usage_signals.MARKER_HOT, kwargs["subject"])
		self.assertIn(self.opp.name, kwargs["message"])
		self.assertEqual(kwargs["reference_name"], self.opp.name)

	def test_daily_report_lists_todays_trials_and_goes_to_the_team(self):
		payload = {"invoice_count": 6, "first_invoice_date": nowdate(), "user_count": 2, "last_login": frappe.utils.now(),
		           "invoice_days_7d": 3, "invoice_days_prev_7d": 1, "invoice_count_30d": 6,
		           "active_users_7d": 2, "master_data_count": 12, "other_docs_30d": 1}
		with patch("ai_saas.saas.usage_signals.run_cmd_capture", return_value=self._bench_output(payload)), \
		     patch("ai_saas.saas.crm.frappe.sendmail"), \
		     patch("ai_saas.saas.usage_signals.send_daily_usage_report") as report:
			usage_signals.collect_usage_snapshots(contracts=[self.contract.name])
			report.assert_not_called()  # a scoped re-run never reports
			usage_signals.collect_usage_snapshots()
			report.assert_called_once()  # the daily sweep does

		rows = [r for r in usage_signals.usage_report_rows() if r.contract == self.contract.name]
		self.assertEqual(len(rows), 1)
		self.assertEqual((rows[0].signal, rows[0].engagement_score, rows[0].opportunity), ("Engaged", 6, self.opp.name))
		self.assertEqual(rows[0].days_left, 14)

		frappe.db.set_single_value("MZ SaaS Settings", "usage_report_recipients", "vendas@example.com, gestor@example.com")
		try:
			with patch("ai_saas.saas.usage_signals.frappe.sendmail") as sendmail:
				self.assertTrue(usage_signals.send_daily_usage_report())
			kwargs = sendmail.call_args.kwargs
			self.assertEqual(kwargs["recipients"], ["vendas@example.com", "gestor@example.com"])
			self.assertIn("quentes", kwargs["subject"])
			self.assertIn(TEST_CUSTOMER, kwargs["message"])
			self.assertIn("🔥 Quente", kwargs["message"])
		finally:
			frappe.db.set_single_value("MZ SaaS Settings", "usage_report_recipients", "")

	def test_find_opportunity_resolves_customer_and_none(self):
		self.assertEqual(crm.find_opportunity(self.contract.name), self.opp.name)
		frappe.db.set_value("Opportunity", self.opp.name, "status", "Lost")
		self.assertIsNone(crm.find_opportunity(self.contract.name))
