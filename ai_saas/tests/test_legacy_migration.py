"""The inventory reads; it never guesses. These tests pin the matching keys, the
classification and the fact that a run leaves nothing behind but the File."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

from ai_saas.saas import legacy_migration as lm
from ai_saas.tests.helpers import cleanup_contract, ensure_test_plan, make_test_customer

SITE = "inventario-teste.erp.mozeconomia.co.mz"
EMAIL = "inventario-teste@example.com"
NUIT = "400123456"


def _ident(**over):
	base = {
		"company": {"company_name": "Inventário Teste, LDA", "tax_id": NUIT, "email": None, "phone_no": None},
		"people": [{"full_name": "Ana Teste", "email": EMAIL, "mobile_no": "+258 84 000 0001", "last_login": None}],
		"usage": {"invoice_count": 0, "master_data_count": 0, "user_count": 1, "last_login": None},
		"db_size_mb": 1.0, "error": "",
	}
	base.update(over)
	return base


class TestLegacyMigration(FrappeTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		ensure_test_plan()

	def setUp(self):
		self.customer = make_test_customer("Inventário Teste, LDA")
		frappe.db.set_value("Customer", self.customer, {"tax_id": NUIT, "email_id": EMAIL, "mobile_no": "840000001"})
		self.contract = frappe.get_doc({
			"doctype": "Contract", "party_type": "Customer", "party_name": self.customer,
			"start_date": nowdate(), "mz_tenant": "inventario-teste", "mz_tenant_url": SITE,
			"contract_terms": "t", "mz_subscription_plan": "Premium Mensal - MozEconomia Cloud",
		})
		with patch("ai_saas.saas.provisioning.provision_tenant"):
			self.contract.insert(ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		cleanup_contract(self.contract.name)
		for f in frappe.get_all("File", filters={"attached_to_doctype": "MZ SaaS Settings", "file_name": ("like", "tenant_inventory_%")}, pluck="name"):
			frappe.delete_doc("File", f, force=True, ignore_permissions=True)
		frappe.delete_doc("Customer", self.customer, force=True, ignore_permissions=True)
		frappe.db.commit()

	def test_matches_by_site_nuit_email_and_name(self):
		hits = lm._match(SITE, _ident())
		self.assertEqual(hits["customers"][self.customer], ["email", "mobile", "name", "nuit", "site"])
		self.assertIn(self.contract.name, hits["contracts"])

	def test_matches_by_fuzzy_name_only(self):
		ident = _ident(company={"company_name": "Inventario Teste Limitada", "tax_id": None, "email": None, "phone_no": None}, people=[])
		hits = lm._match("outro.erp.mozeconomia.co.mz", ident)
		self.assertEqual(hits["customers"].get(self.customer), ["name"])
		best, quality, _ = lm._pick(hits["customers"])
		self.assertEqual((best, quality), (self.customer, "name-only"))

	def test_pick_prefers_strong_keys_and_reports_conflicts(self):
		best, quality, conflicts = lm._pick({"A": ["name"], "B": ["email"], "C": ["site", "nuit"]})
		self.assertEqual((best, quality), ("C", "strong"))
		self.assertIn("B (email)", conflicts)
		self.assertNotIn("A", conflicts)
		self.assertEqual(lm._pick({}), (None, "none", ""))

	def test_control_side_never_borrows_another_customers_contract(self):
		other = make_test_customer("Outro Cliente, LDA")
		try:
			out = lm._control_side(other, {self.contract.name: ["email"]}, {}, {})
			self.assertIsNone(out["contract"])
			out = lm._control_side(None, {self.contract.name: ["site"]}, {}, {})
			self.assertEqual((out["customer"], out["contract"]), (self.customer, self.contract.name))
		finally:
			frappe.delete_doc("Customer", other, force=True, ignore_permissions=True)

	def test_classification_reads_the_signals(self):
		c = lm._classify
		self.assertEqual(c({"site_dir": "archived"})[0], "archived_by_hand")
		self.assertEqual(c({"site_dir": "live", "probe_error": "boom"})[0], "unclassified")
		self.assertEqual(c({"site_dir": "live", "sub_status": "Active", "outstanding": 0})[0], "paying")
		self.assertEqual(c({"site_dir": "live", "sub_status": "Active", "outstanding": 500, "last_paid_on": add_days(nowdate(), -10)})[0], "paying")
		self.assertEqual(c({"site_dir": "live", "sub_status": "Active", "outstanding": 500, "last_paid_on": add_days(nowdate(), -90)})[0], "debtor_live")
		self.assertEqual(c({"site_dir": "live", "sub_status": "Cancelled", "customer": "X"})[0], "debtor_live")
		self.assertEqual(c({"site_dir": "live", "customer": None})[0], "unmatched_site")
		self.assertEqual(c({"site_dir": "live", "customer": "X", "invoice_count": 0, "master_data_count": 0})[0], "never_used")
		self.assertEqual(c({"site_dir": "live", "customer": "X", "invoice_count": 3})[0], "used_unsigned")

	def test_inventory_attaches_one_file_and_writes_nothing_else(self):
		sites = [{"site": SITE, "site_dir": "live", "path": "/nowhere", "maintenance_mode": 0, "last_backup_on": None}]
		versions = frappe.db.count("Version")
		patches = (
			patch.object(lm, "_sites", return_value=sites),
			patch.object(lm, "_probe_identity", return_value=_ident()),
			patch.object(lm, "_http_status", return_value="200"),
		)
		with patches[0], patches[1], patches[2]:
			result = lm.inventory()
			result2 = lm.inventory()
			row = lm.build()["sites"][0]
		self.assertEqual(result["sites"], 1)
		self.assertTrue(result["file_url"].endswith(".xlsx"))
		self.assertEqual(result2["file_url"], result["file_url"])
		files = frappe.get_all("File", filters={"attached_to_doctype": "MZ SaaS Settings", "file_name": ("like", "tenant_inventory_%")})
		self.assertEqual(len(files), 1)
		self.assertEqual(frappe.db.count("Version"), versions)
		self.assertEqual((row["customer"], row["contract"], row["class"]), (self.customer, self.contract.name, "never_used"))
		self.assertEqual(row["match_quality"], "strong")
		self.assertIn("site", row["match_keys"])
		names = [r["name"] for r in lm._control_only({"customers": {self.customer}, "opportunities": set(), "leads": set()})]
		self.assertNotIn(self.customer, names)
