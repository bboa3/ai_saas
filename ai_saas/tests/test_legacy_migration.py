"""The inventory reads; it never guesses. These tests pin the matching keys, the
classification and the fact that a run leaves nothing behind but the File."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

from ai_saas.saas import legacy_migration as lm
from ai_saas.tests.helpers import cleanup_contract, ensure_test_plan, make_test_customer

SITE = "inventario-teste.erp.mozeconomia.co.mz"
SITE2 = "conta-directa-teste.erp.mozeconomia.co.mz"
DIRECT_CUSTOMER = "Conta Directa Teste, LDA"
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
		for prov in frappe.get_all("MZ Tenant Provisioning", {"site_name": ("in", [SITE, SITE2])}, pluck="name"):
			frappe.delete_doc("MZ Tenant Provisioning", prov, force=True, ignore_permissions=True)
		for c in frappe.get_all("Contract", {"party_name": DIRECT_CUSTOMER}, pluck="name"):
			cleanup_contract(c)
		cleanup_contract(self.contract.name)
		for party in (self.customer, DIRECT_CUSTOMER):
			for sub in frappe.get_all("Subscription", {"party": party}, pluck="name"):
				frappe.delete_doc("Subscription", sub, force=True, ignore_permissions=True)
			for opp in frappe.get_all("Opportunity", {"party_name": party}, pluck="name"):
				frappe.delete_doc("Opportunity", opp, force=True, ignore_permissions=True)
		for dt in ("Contact", "Address"):
			for n in frappe.get_all("Dynamic Link", {"link_doctype": "Customer", "link_name": DIRECT_CUSTOMER, "parenttype": dt}, pluck="parent"):
				frappe.delete_doc(dt, n, force=True, ignore_permissions=True)
		frappe.delete_doc("Customer", DIRECT_CUSTOMER, force=True, ignore_permissions=True)
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
		self.assertEqual(c({"site_dir": "live", "sub_status": "Active", "outstanding": 500, "last_paid_on": add_days(nowdate(), -90)})[0], "debtor")
		self.assertEqual(c({"site_dir": "live", "sub_status": "Cancelled", "outstanding": 300, "customer": "X"})[0], "debtor")
		self.assertEqual(c({"site_dir": "live", "sub_status": "Cancelled", "outstanding": 0, "customer": "X"})[0], "cancelled_paid_up")
		self.assertEqual(c({"site_dir": "live", "customer": None})[0], "unmatched_site")
		self.assertEqual(c({"site_dir": "live", "customer": "X", "invoice_count": 0, "master_data_count": 0})[0], "never_used")
		self.assertEqual(c({"site_dir": "live", "customer": "X", "invoice_count": 3})[0], "used_unsigned")

	def test_inventory_attaches_one_file_and_writes_nothing_else(self):
		sites = [{"site": SITE, "site_dir": "live", "path": "/nowhere", "maintenance_mode": 0, "last_backup_on": None},
		         {"site": SITE, "site_dir": "archived", "path": "/nowhere2", "maintenance_mode": 0, "last_backup_on": None}]
		versions = frappe.db.count("Version")
		patches = (
			patch.object(lm, "_sites", return_value=sites),
			patch.object(lm, "_probe_identity", return_value=_ident()),
			patch.object(lm, "_http_status", return_value="200"),
		)
		with patches[0], patches[1], patches[2]:
			result = lm.inventory()
			result2 = lm.inventory()
			rows = lm.build()["sites"]
		row = rows[0]
		self.assertEqual(rows[1]["class"], "stale_archived_copy")
		self.assertEqual(result["sites"], 2)
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

	def _live_sites(self, *sites):
		return [{"site": x, "site_dir": "live", "path": "/nowhere", "maintenance_mode": 0, "last_backup_on": None} for x in sites]

	def _submit_contract(self, signed=False):
		with patch("ai_saas.saas.provisioning.provision_tenant"):
			self.contract.submit()
		if signed:
			frappe.db.set_value("Contract", self.contract.name, "is_signed", 1, update_modified=False)
		frappe.db.commit()

	def _make_subscription(self, start):
		from ai_saas.saas.contract_lifecycle import _get_company
		from ai_saas.tests.helpers import TEST_PLAN

		return frappe.get_doc({
			"doctype": "Subscription", "party_type": "Customer", "party": self.customer,
			"company": _get_company(), "start_date": start,
			"generate_invoice_at": "Beginning of the current subscription period",
			"submit_invoice": 1, "days_until_due": 7, "plans": [{"plan": TEST_PLAN, "qty": 1}],
		}).insert(ignore_permissions=True)

	def test_activate_links_the_records_it_finds_and_sends_nothing(self):
		from frappe.utils import add_days, cint, getdate, nowdate

		sub = self._make_subscription(add_days(nowdate(), -30))
		self._submit_contract(signed=True)
		queued, subs = frappe.db.count("Email Queue"), frappe.db.count("Subscription")

		with patch.object(lm, "_sites", return_value=self._live_sites(SITE)):
			dry = lm.activate(self.contract.name, dry_run=1)
			self.assertIn("[dry-run]", dry[0])
			self.assertFalse(frappe.db.exists("MZ Tenant Provisioning", {"contract": self.contract.name}))
			lm.activate(self.contract.name, dry_run=0)
			again = lm.activate(self.contract.name, dry_run=0)

		prov = frappe.db.get_value("MZ Tenant Provisioning", {"contract": self.contract.name}, ["status", "site_name"], as_dict=True)
		self.assertEqual((prov.status, prov.site_name), ("Active", SITE))
		c = frappe.db.get_value("Contract", self.contract.name, ["mz_linked_subscription", "mz_billing_start", "mz_direct"], as_dict=True)
		self.assertEqual((c.mz_linked_subscription, getdate(c.mz_billing_start), cint(c.mz_direct)), (sub.name, getdate(sub.start_date), 1))
		opp = frappe.db.get_value("Opportunity", {"party_name": self.customer}, ["sales_stage", "status"], as_dict=True)
		self.assertEqual((opp.sales_stage, opp.status), ("Cloud - Activated", "Converted"))
		self.assertEqual(frappe.db.count("Email Queue"), queued)  # nothing mailed to anyone
		self.assertEqual(frappe.db.count("Subscription"), subs)  # never a second subscription
		self.assertEqual(len(again), 1)  # idempotent second run, same single digest line

		from ai_saas.saas.tenant_lifecycle import account_phase
		self.assertEqual(account_phase(self.contract.name), "Active")

	def test_activate_refuses_what_it_should(self):
		self._submit_contract(signed=False)
		with patch.object(lm, "_sites", return_value=self._live_sites(SITE)):
			out = lm.activate(self.contract.name, dry_run=0)  # unsigned
		self.assertIn("IGNORADO", out[0])
		self.assertFalse(frappe.db.exists("MZ Tenant Provisioning", {"contract": self.contract.name}))

	def test_create_account_registers_first_and_never_backdates(self):
		from frappe.utils import add_days, cint, getdate, nowdate

		queued = frappe.db.count("Email Queue")
		start = add_days(nowdate(), 7)
		ident = _ident(company={"company_name": DIRECT_CUSTOMER, "tax_id": "400999888", "email": None, "phone_no": None},
		               people=[{"full_name": "Gestor Directo", "email": "gestor-directo@example.com", "mobile_no": "+258 84 000 0003", "last_login": None}])
		with patch.object(lm, "_sites", return_value=self._live_sites(SITE, SITE2)), 		     patch.object(lm, "_probe_identity", return_value=ident):
			from ai_saas.tests.helpers import TEST_PLAN

			line = lm.create_account(DIRECT_CUSTOMER, SITE2, TEST_PLAN, start, dry_run=1)
			self.assertIn("[dry-run]", line)
			self.assertFalse(frappe.db.exists("Customer", {"customer_name": DIRECT_CUSTOMER}))
			result = lm.create_account(DIRECT_CUSTOMER, SITE2, TEST_PLAN, start, dry_run=0)
			self.assertRaisesRegex(frappe.ValidationError, "já pertence",
			                       lm.create_account, DIRECT_CUSTOMER, SITE2, None, None, None, 0)

		contract = frappe.get_doc("Contract", result["contract"])
		self.assertEqual((contract.docstatus, cint(contract.is_signed), cint(contract.mz_direct)), (1, 1, 1))
		sub = frappe.get_doc("Subscription", result["subscription"])
		self.assertEqual(getdate(sub.start_date), getdate(start))
		self.assertFalse(frappe.db.exists("Sales Invoice", {"customer": result["customer"], "docstatus": ("<", 2)}))
		prov = frappe.db.get_value("MZ Tenant Provisioning", {"contract": contract.name}, "status")
		self.assertEqual(prov, "Active")
		self.assertEqual(frappe.db.count("Email Queue"), queued)  # no delivery email: nothing provisioned
		opp = frappe.db.get_value("Opportunity", {"party_name": result["customer"]}, ["sales_stage", "status"], as_dict=True)
		self.assertEqual((opp.sales_stage, opp.status), ("Cloud - Activated", "Converted"))

	def test_create_account_bills_the_contracted_seats(self):
		"""A direct account sold with N seats bills N-1 plan costs from day one — the
		seats must reach the Subscription through the submit hook, not a later edit."""
		from frappe.utils import cint

		from ai_saas.tests.helpers import TEST_PLAN

		# Future start date: billing from today would issue the first invoice here (E2),
		# and a submitted invoice is not something a test should leave behind.
		start = add_days(nowdate(), 7)
		with patch.object(lm, "_sites", return_value=self._live_sites(SITE2)), \
		     patch.object(lm, "_probe_identity", return_value=_ident()):
			line = lm.create_account(DIRECT_CUSTOMER, SITE2, TEST_PLAN, start, dry_run=1, users=6)
			self.assertIn("6 utilizadores → 5 x", line)  # the money is visible before the write
			result = lm.create_account(DIRECT_CUSTOMER, SITE2, TEST_PLAN, start, dry_run=0, users=6)

		self.assertEqual(cint(frappe.db.get_value("Contract", result["contract"], "mz_users")), 6)
		sub = frappe.get_doc("Subscription", result["subscription"])
		self.assertEqual([row.qty for row in sub.plans], [5])

	def test_create_account_refuses_seats_it_cannot_bill(self):
		from ai_saas.tests.helpers import TEST_PLAN

		with patch.object(lm, "_sites", return_value=self._live_sites(SITE2)), \
		     patch.object(lm, "_probe_identity", return_value=_ident()):
			self.assertRaisesRegex(frappe.ValidationError, "pelo menos 1 utilizador",
			                       lm.create_account, DIRECT_CUSTOMER, SITE2, TEST_PLAN, None, None, 0, 0)
			self.assertRaisesRegex(frappe.ValidationError, "sem plano",
			                       lm.create_account, DIRECT_CUSTOMER, SITE2, None, None, None, 0, 6)
		self.assertFalse(frappe.db.exists("Contract", {"party_name": DIRECT_CUSTOMER}))

	def test_prepare_account_stops_at_the_draft_and_adopts_the_site(self):
		"""The half-verb: every document except the one that bills. The contract must be
		a draft with no Subscription, and the provisioning row must already point at the
		existing site — that row is what makes the later submit adopt it instead of
		building a second one."""
		from frappe.utils import add_days, cint, nowdate

		from ai_saas.tests.helpers import TEST_PLAN

		queued = frappe.db.count("Email Queue")
		start = add_days(nowdate(), 7)
		with patch.object(lm, "_sites", return_value=self._live_sites(SITE2)), \
		     patch.object(lm, "_probe_identity", return_value=_ident()):
			out = lm.prepare_account(DIRECT_CUSTOMER, SITE2, TEST_PLAN, start, dry_run=0, users=6)

		contract = frappe.get_doc("Contract", out["contract"])
		self.assertEqual(contract.docstatus, 0)  # draft: the human submits it
		self.assertEqual(cint(contract.is_signed), 1)
		self.assertEqual(cint(contract.mz_users), 6)
		self.assertFalse(contract.get("mz_linked_subscription"))
		self.assertFalse(frappe.db.exists("Subscription", {"party": out["customer"]}))
		prov = frappe.db.get_value("MZ Tenant Provisioning", out["provisioning"],
		                           ["contract", "site_name", "status"], as_dict=True)
		self.assertEqual((prov.contract, prov.site_name, prov.status), (contract.name, SITE2, "Active"))
		self.assertTrue(out["opportunity"])
		self.assertEqual(frappe.db.count("Email Queue"), queued)  # nothing provisions, nothing mails

		# The submit the operator would click, with the REAL provision_tenant: the row it
		# finds makes it return before validate_slug, so no site is built and no second
		# row appears — that early return is exactly what adopting an existing site means.
		rows = frappe.db.count("MZ Tenant Provisioning")
		contract.submit()
		self.assertEqual(frappe.db.count("MZ Tenant Provisioning"), rows)
		self.assertEqual(frappe.db.get_value("MZ Tenant Provisioning", out["provisioning"], "status"), "Active")
		sub = frappe.db.get_value("Contract", contract.name, "mz_linked_subscription")
		self.assertTrue(sub)
		self.assertEqual([row.qty for row in frappe.get_doc("Subscription", sub).plans], [5])

	def test_prepare_account_reports_what_the_invoice_would_lack(self):
		from ai_saas.tests.helpers import TEST_PLAN

		with patch.object(lm, "_sites", return_value=self._live_sites(SITE2)), \
		     patch.object(lm, "_probe_identity", return_value=_ident()):
			out = lm.prepare_account(DIRECT_CUSTOMER, SITE2, TEST_PLAN, dry_run=0, users=6)
		# The identity probe carries no address, so that gap is always reported.
		self.assertTrue(any("Endereço" in g for g in out["gaps"]))
	def test_create_account_without_plan_is_engine_silent_but_converted(self):
		"""The holding/partner shape: no Subscription, no billing — but the ledger still
		says what the account is (Activated/Converted), which on_contract_submitted alone
		would skip for a plan-less contract."""
		with patch.object(lm, "_sites", return_value=self._live_sites(SITE2)), \
		     patch.object(lm, "_probe_identity", return_value=_ident()):
			result = lm.create_account(DIRECT_CUSTOMER, SITE2, None, None, "parceiro@example.com", dry_run=0)
		self.assertIsNone(result["subscription"])
		self.assertFalse(frappe.db.exists("Subscription", {"party": result["customer"]}))
		opp = frappe.db.get_value("Opportunity", {"party_name": result["customer"]}, ["sales_stage", "status"], as_dict=True)
		self.assertEqual((opp.sales_stage, opp.status), ("Cloud - Activated", "Converted"))
		from ai_saas.saas.tenant_lifecycle import account_phase, live_trials
		self.assertEqual(account_phase(result["contract"]), "Active")
		self.assertNotIn(result["contract"], [r.name for r in live_trials()])

	def test_direct_contracts_are_invisible_to_the_trial_engine(self):
		from ai_saas.saas.tenant_lifecycle import live_trials

		self._submit_contract(signed=False)
		frappe.get_doc({
			"doctype": "MZ Tenant Provisioning", "contract": self.contract.name,
			"tenant_slug": "inventario-teste", "site_name": SITE, "status": "Active",
		}).insert(ignore_permissions=True)
		self.assertIn(self.contract.name, [r.name for r in live_trials()])
		frappe.db.set_value("Contract", self.contract.name, "mz_direct", 1, update_modified=False)
		self.assertNotIn(self.contract.name, [r.name for r in live_trials()])

	def _armed_trial(self):
		self._submit_contract(signed=False)
		frappe.get_doc({
			"doctype": "MZ Tenant Provisioning", "contract": self.contract.name,
			"tenant_slug": "inventario-teste", "site_name": SITE, "status": "Active",
		}).insert(ignore_permissions=True)
		opp = frappe.get_doc({
			"doctype": "Opportunity", "opportunity_from": "Customer", "party_name": self.customer,
			"company": frappe.db.get_single_value("Global Defaults", "default_company"),
			"sales_stage": "Cloud - Account Created", "contact_email": EMAIL,
		}).insert(ignore_permissions=True)
		frappe.db.commit()
		return opp.name

	def _archive_patches(self):
		import os as _os

		return (
			patch("ai_saas.saas.tenant_lifecycle.run_cmd"),
			patch("ai_saas.saas.tenant_lifecycle._run"),
			patch("ai_saas.saas.tenant_lifecycle._has_recent_backup", return_value=True),
			patch("ai_saas.saas.tenant_lifecycle.os.path.isdir", return_value=True),
			patch("ai_saas.saas.tenant_lifecycle.get_db_root_password", return_value="x"),
		)

	def test_archive_now_one_email_then_the_campaign(self):
		from frappe.utils import strip_html

		opp = self._armed_trial()
		queued = frappe.db.count("Email Queue")
		dry = lm.archive_now(self.contract.name, dry_run=1)
		self.assertIn("[dry-run]", dry[0])
		p1, p2, p3, p4, p5 = self._archive_patches()
		with p1, p2, p3, p4, p5:
			out = lm.archive_now(self.contract.name, dry_run=0)
		self.assertIn("arquivado", out[0])
		# suspend was silent; archive sent exactly one "Conta Arquivada"
		self.assertEqual(frappe.db.count("Email Queue"), queued + 1)
		stage = frappe.db.get_value("Opportunity", opp, ["sales_stage", "status", "mz_stage_since"], as_dict=True)
		self.assertEqual((stage.sales_stage, stage.status), ("Cloud - Closed", "Lost"))
		self.assertTrue(stage.mz_stage_since)  # the G3 clock is running
		again = lm.archive_now(self.contract.name, dry_run=0)
		self.assertIn("já arquivado", again[0])

	def test_archive_now_quiet_sends_nothing_ever(self):
		opp = self._armed_trial()
		queued = frappe.db.count("Email Queue")
		p1, p2, p3, p4, p5 = self._archive_patches()
		with p1, p2, p3, p4, p5:
			lm.archive_now(self.contract.name, dry_run=0, quiet=1)
		self.assertEqual(frappe.db.count("Email Queue"), queued)
		stage = frappe.db.get_value("Opportunity", opp, ["sales_stage", "status", "mz_stage_since"], as_dict=True)
		self.assertEqual((stage.sales_stage, stage.status, stage.mz_stage_since), ("Cloud - Closed", "Lost", None))

	def test_archive_now_refuses_the_unregistered(self):
		self._submit_contract(signed=False)  # no provisioning row
		out = lm.archive_now(self.contract.name, dry_run=0)
		self.assertIn("IGNORADO", out[0])
