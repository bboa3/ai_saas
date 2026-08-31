"""Apps per segment (2026-08-28): base on new-site, the segment's extras one by one after,
a failing extra never costs the customer the account."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ai_saas.saas import provisioning as P

SEG = "_Test Segmento Apps"


class _Prov(frappe._dict):
	def save(self, **kw): pass
	def get(self, k, default=None): return frappe._dict.get(self, k, default)


class TestAppsForSegment(FrappeTestCase):
	def setUp(self):
		if not frappe.db.exists("Segment Intelligence Map", SEG):
			frappe.get_doc({"doctype": "Segment Intelligence Map", "segmento": SEG,
			                "aplicacoes": [{"app_name": "healthcare"}, {"app_name": "hrms"}, {"app_name": "nope_app"}, {"app_name": "erpnext"}]}
			               ).insert(ignore_permissions=True)
		self.bench = patch.object(P, "available_apps", return_value=["erpnext", "erpnext_mz", "hrms", "healthcare", "pos_next", "payments", "curati_connect"])
		self.bench.start()

	def tearDown(self):
		self.bench.stop()
		frappe.delete_doc("Segment Intelligence Map", SEG, force=True, ignore_permissions=True)

	def test_base_only_without_segment(self):
		self.assertEqual(P.apps_for_segment(None), ["erpnext", "erpnext_mz"])
		self.assertEqual(P.apps_for_segment("Segmento Inexistente"), ["erpnext", "erpnext_mz"])

	def test_segment_apps_in_install_order_unknown_dropped(self):
		# hrms before erpnext_mz (INSTALL_BEFORE_MZ), base, then extras in segment order; nope_app gone, erpnext not doubled
		self.assertEqual(P.apps_for_segment(SEG, "Premium Mensal - MozEconomia Cloud"), ["hrms", "erpnext", "erpnext_mz", "healthcare"])

	def test_hrms_only_on_paid_tiers(self):
		for plan, expect_hrms in (("Básico Mensal - MozEconomia Cloud", False), ("_Test Basico - MozEconomia Cloud", False), (None, False),
		                          ("Profissional Mensal - MozEconomia Cloud", True), ("Premium Anual - MozEconomia Cloud", True)):
			apps = P.apps_for_segment(SEG, plan)
			self.assertEqual("hrms" in apps, expect_hrms, plan)
			self.assertIn("healthcare", apps, plan)  # not gated
		self.assertEqual(P.plan_tier("Premium Mensal - MozEconomia Cloud"), "Premium")
		self.assertEqual(P.plan_tier("_Test Basico - MozEconomia Cloud"), "Básico")
		self.assertEqual(P.plan_tier("Ceres 12x1L - Mensal"), "")

	def test_domain_profile_adds_partner_apps_after_the_segment(self):
		"""Curati (2026-08-29): pharmacy apps on top of the segment's, healthcare before
		curati_connect (its required_app); other domains add nothing; unknown → default."""
		self.assertEqual(P.apps_for_segment(SEG, "Premium Mensal - MozEconomia Cloud", ".erp.curati.co.mz"),
		                 ["hrms", "erpnext", "erpnext_mz", "healthcare", "pos_next", "curati_connect"])
		self.assertEqual(P.apps_for_segment(None, None, ".erp.curati.co.mz"),
		                 ["erpnext", "erpnext_mz", "healthcare", "pos_next", "curati_connect"])
		self.assertEqual(P.apps_for_segment(SEG, None, ".erp.kalenyholding.com"), P.apps_for_segment(SEG, None))
		self.assertEqual(P.domain_for(".erp.kalenyholding.com"), ".erp.kalenyholding.com")
		self.assertEqual(P.domain_for("evil.com"), P.DEFAULT_DOMAIN)
		self.assertEqual(P.domain_for(None), P.DEFAULT_DOMAIN)
		self.assertEqual(P.domain_profile(".erp.curati.co.mz")["segment"], "Saúde & Bem-Estar")
		self.assertTrue(frappe.db.exists("Segment Intelligence Map", "Saúde & Bem-Estar"))
		for d in P.DOMAINS:
			self.assertIn(d, P.ROUTE_BY_DOMAIN)

	def test_every_segment_lists_hrms(self):
		import json
		with open(frappe.get_app_path("ai_saas", "fixtures", "segment_intelligence_map.json"), encoding="utf-8") as f:
			for seg in json.load(f):
				self.assertIn("hrms", [r["app_name"] for r in seg.get("aplicacoes", [])], seg["name"])

	def test_split_site_and_extra(self):
		self.assertEqual(P.split_site_and_extra_apps(["hrms", "erpnext", "erpnext_mz", "healthcare", "pos_next"]),
		                 (["hrms", "erpnext", "erpnext_mz"], ["healthcare", "pos_next"]))
		self.assertEqual(P.split_site_and_extra_apps(["erpnext", "erpnext_mz"]), (["erpnext", "erpnext_mz"], []))

	def test_available_apps_excludes_platform_apps(self):
		self.bench.stop()
		try:
			apps = P.available_apps()
		finally:
			self.bench.start()
		self.assertIn("erpnext_mz", apps)
		for a in P.EXCLUDED_APPS:
			self.assertNotIn(a, apps)


class TestInstallStep(FrappeTestCase):
	def _prov(self, apps):
		return _Prov(name="MZ-PROV-TEST", site_name="t.erp.mozeconomia.co.mz", log="",
		             mz_provisioning_apps=[frappe._dict(app_name=a) for a in apps])

	def test_new_site_gets_only_site_apps_in_order(self):
		prov = self._prov(["erpnext", "erpnext_mz", "hrms", "healthcare"])
		with patch.object(P, "run_cmd") as run, patch.object(P, "os") as os_, \
		     patch.object(P, "get_db_root_user", return_value="root"), patch.object(P, "get_db_root_password", return_value="x"), \
		     patch.object(P, "_get_site_admin_password", return_value="y"), patch.object(P, "get_bench_cmd", return_value="bench"), \
		     patch.object(frappe.db, "commit"):
			os_.path.exists.return_value = False
			os_.path.join.return_value = "/x"
			P._step_create_site(prov)
		cmd = run.call_args_list[0].args[0]
		flags = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--install-app"]
		self.assertEqual(flags, ["hrms", "erpnext", "erpnext_mz"])  # healthcare is not on new-site

	def test_extras_one_by_one_failure_isolated_and_reported(self):
		prov = self._prov(["erpnext", "erpnext_mz", "healthcare", "pos_next", "payments"])
		def run(cmd, step, prov, timeout):
			if "pos_next" in cmd:
				raise P.ProvisioningError("boom")
		with patch.object(P, "run_cmd", side_effect=run) as r, patch.object(P, "run_cmd_capture", return_value="frappe\nerpnext\nerpnext_mz\npayments\n"), \
		     patch.object(P, "notify_ops") as ops, patch.object(P, "get_bench_cmd", return_value="bench"), patch.object(frappe.db, "commit"):
			P._step_install_apps(prov)
		installed = [c.args[0][-1] for c in r.call_args_list]
		self.assertEqual(installed, ["healthcare", "pos_next"])            # payments already on the site → skipped
		self.assertIn("AVISO: app pos_next não instalada", prov.log)
		self.assertIn("App healthcare instalada.", prov.log)
		ops.assert_called_once()
		self.assertIn("pos_next", ops.call_args.args[0])

	def test_no_extras_is_a_noop(self):
		with patch.object(P, "run_cmd_capture") as cap:
			P._step_install_apps(self._prov(["erpnext", "erpnext_mz"]))
		cap.assert_not_called()
