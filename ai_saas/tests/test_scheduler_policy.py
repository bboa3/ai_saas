"""C4: the tenant scheduler runs only for plans listed in MZ SaaS Settings — provisioning sets it per plan,
and signature re-applies it (the plan may change at activation). Bench commands patched."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

from ai_saas.saas import provisioning, tenant_lifecycle
from ai_saas.tests.helpers import (
	BASIC_PLAN,
	TEST_PLAN,
	cleanup_contract,
	ensure_test_plan,
	make_test_customer,
)

TEST_CUSTOMER = "_Test Cliente AI SaaS SCH"
SLUG = "sch-teste"


class TestSchedulerPolicy(FrappeTestCase):
	def setUp(self):
		ensure_test_plan()
		make_test_customer(TEST_CUSTOMER)
		# The scheduler is on for the plans listed in MZ SaaS Settings.scheduler_plans.
		real = tenant_lifecycle.get_settings()
		real.scheduler_plans = [TEST_PLAN]
		self._settings = patch("ai_saas.saas.tenant_lifecycle.get_settings", return_value=real)
		self._settings.start()
		with patch("ai_saas.saas.provisioning.provision_tenant"):
			self.contract = frappe.get_doc({
				"doctype": "Contract", "party_type": "Customer", "party_name": TEST_CUSTOMER,
				"start_date": add_days(nowdate(), 14), "contract_terms": "Termos SCH.",
				"mz_subscription_plan": BASIC_PLAN, "mz_tenant": SLUG,
			}).insert(ignore_permissions=True)
			self.contract.submit()
		self.prov = frappe.get_doc({
			"doctype": "MZ Tenant Provisioning", "contract": self.contract.name, "tenant_slug": SLUG,
			"site_name": f"{SLUG}.erp.mozeconomia.co.mz", "status": "Active",
		}).insert(ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		frappe.delete_doc("MZ Tenant Provisioning", self.prov.name, force=True, ignore_missing=True, ignore_permissions=True)
		cleanup_contract(self.contract.name)
		self._settings.stop()
		frappe.delete_doc("Customer", TEST_CUSTOMER, force=True, ignore_missing=True, ignore_permissions=True)
		frappe.db.commit()

	def test_plan_flag_decides(self):
		self.assertTrue(provisioning.scheduler_enabled_for_plan(TEST_PLAN))
		self.assertFalse(provisioning.scheduler_enabled_for_plan(BASIC_PLAN))
		self.assertFalse(provisioning.scheduler_enabled_for_plan(None))

	@patch("ai_saas.saas.provisioning.run_cmd")
	def test_provisioning_step_disables_for_non_premium(self, run_cmd):
		provisioning._step_apply_scheduler_policy(self.prov)
		cmd = run_cmd.call_args[0][0]
		self.assertEqual(cmd[-1], "disable-scheduler")
		self.assertIn(self.prov.site_name, cmd)
		self.assertIn("DESACTIVADO", self.prov.log)
		self.assertIn(provisioning._step_apply_scheduler_policy, provisioning.PROVISIONING_STEPS)

	@patch("ai_saas.saas.provisioning.run_cmd")
	def test_signature_reapplies_after_plan_change(self, run_cmd):
		# The customer corrects the plan to Premium at activation, then signs.
		doc = frappe.get_doc("Contract", self.contract.name)
		doc.mz_subscription_plan = TEST_PLAN
		doc.is_signed = 1
		with patch("ai_saas.saas.contract_lifecycle._setup_subscription"):
			doc.save(ignore_permissions=True)
		cmds = [c.args[0][-1] for c in run_cmd.call_args_list]
		self.assertEqual(cmds, ["enable-scheduler"])
		self.assertIn("assinatura", frappe.db.get_value("MZ Tenant Provisioning", self.prov.name, "log"))

	@patch("ai_saas.saas.provisioning.run_cmd", side_effect=provisioning.ProvisioningError("bench down"))
	def test_signature_survives_a_failing_bench(self, run_cmd):
		doc = frappe.get_doc("Contract", self.contract.name)
		doc.is_signed = 1
		with patch("ai_saas.saas.contract_lifecycle._setup_subscription"), patch("ai_saas.saas.alerts.notify_ops") as alert:
			doc.save(ignore_permissions=True)
		alert.assert_called_once()
		self.assertEqual(frappe.db.get_value("Contract", self.contract.name, "is_signed"), 1)
