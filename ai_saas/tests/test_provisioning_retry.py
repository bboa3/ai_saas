"""Tests for C1 (a Failed provisioning is retryable) and C3 (alert recipients
come from MZ SaaS Settings). frappe.enqueue is patched — nothing is ever run."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

from ai_saas.saas import provisioning

TEST_CUSTOMER = "_Test Cliente AI SaaS C"
TEST_PLAN = "Premium Mensal - MozEconomia Cloud"
TEST_SLUG = "c1-teste"


class TestProvisioningRetry(FrappeTestCase):
	def setUp(self):
		if not frappe.db.exists("Customer", TEST_CUSTOMER):
			frappe.get_doc({
				"doctype": "Customer",
				"customer_name": TEST_CUSTOMER,
				"customer_type": "Company",
				"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
				"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
			}).insert(ignore_permissions=True)
		self.contract = frappe.get_doc({
			"doctype": "Contract",
			"party_type": "Customer",
			"party_name": TEST_CUSTOMER,
			"start_date": add_days(nowdate(), 14),
			"contract_terms": "Termos de teste C.",
			"mz_subscription_plan": TEST_PLAN,
			"mz_tenant": TEST_SLUG,
		}).insert(ignore_permissions=True)
		self.prov = frappe.get_doc({
			"doctype": "MZ Tenant Provisioning",
			"contract": self.contract.name,
			"tenant_slug": TEST_SLUG,
			"site_name": f"{TEST_SLUG}.erp.mozeconomia.co.mz",
			"status": "Failed",
			"attempts": provisioning.MAX_ATTEMPTS,
			"last_error": "erro de teste",
		}).insert(ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		frappe.delete_doc("MZ Tenant Provisioning", self.prov.name, force=True, ignore_missing=True, ignore_permissions=True)
		frappe.delete_doc("Contract", self.contract.name, force=True, ignore_missing=True, ignore_permissions=True)
		frappe.delete_doc("Customer", TEST_CUSTOMER, force=True, ignore_missing=True, ignore_permissions=True)
		frappe.db.commit()

	# ---- C1 ---------------------------------------------------------------------

	@patch("frappe.enqueue")
	def test_failed_record_is_requeued_not_duplicated(self, enqueue):
		provisioning.provision_tenant(self.contract.name)

		self.prov.reload()
		self.assertEqual(self.prov.status, "Queued")
		self.assertEqual(self.prov.attempts, 0)
		self.assertFalse(self.prov.last_error)
		self.assertIn("contador de tentativas reposto", self.prov.log)
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["provisioning_name"], self.prov.name)
		self.assertEqual(
			frappe.db.count("MZ Tenant Provisioning", {"contract": self.contract.name}), 1
		)

	@patch("frappe.enqueue")
	def test_other_statuses_keep_the_silent_return(self, enqueue):
		frappe.db.set_value("MZ Tenant Provisioning", self.prov.name, "status", "Active")
		provisioning.provision_tenant(self.contract.name)
		enqueue.assert_not_called()
		self.assertEqual(frappe.db.get_value("MZ Tenant Provisioning", self.prov.name, "status"), "Active")

	@patch("frappe.enqueue")
	def test_retry_refuses_non_failed(self, enqueue):
		frappe.db.set_value("MZ Tenant Provisioning", self.prov.name, "status", "Creating Site")
		with self.assertRaises(frappe.ValidationError):
			provisioning.retry_failed_provisioning(self.prov.name)
		enqueue.assert_not_called()

	# ---- C3 ---------------------------------------------------------------------

	def test_alert_recipients_prefer_settings(self):
		with patch("ai_saas.saas.alerts.get_settings") as gs:
			gs.return_value = frappe._dict(ops_alert_recipients=["ops@example.com", "sales@example.com"])
			self.assertEqual(provisioning._ops_alert_recipients(), ["ops@example.com", "sales@example.com"])
		with patch("ai_saas.saas.alerts.get_settings") as gs:
			gs.return_value = frappe._dict(ops_alert_recipients=[])
			fallback = provisioning._ops_alert_recipients()
			self.assertEqual(len(fallback), 1)
			self.assertTrue(fallback[0])
