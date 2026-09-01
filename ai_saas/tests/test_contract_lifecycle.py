"""Tests for the B1-B5 trigger split (docs/sales-funnel-implementation.md, Part B).

Provisioning and Subscription creation are patched out — these tests exercise the
dispatch logic, the phase stamping, the B3 plan guard, the B4 template render and
the B5 primary-contact resolution, not billing or site creation.

Known bench pattern: doc.submit() commits, so tearDown cleans up explicitly —
frappe.db.rollback() is not enough.
"""

from unittest.mock import patch

import frappe
from frappe.utils import add_days, nowdate

from ai_saas.tests.helpers import FunnelTestCase

TEST_CUSTOMER = "_Test Cliente AI SaaS B1"
TEST_PLAN = "Premium Mensal - MozEconomia Cloud"
OTHER_PLAN = "Premium Anual - MozEconomia Cloud"
TEST_SLUG = "b1-teste"


class TestContractLifecycle(FunnelTestCase):
	CUSTOMER = TEST_CUSTOMER

	def _make_contract(self, is_signed=0, tenant=TEST_SLUG, plan=TEST_PLAN, users=None):
		doc = frappe.get_doc({
			"doctype": "Contract",
			"party_type": "Customer",
			"party_name": TEST_CUSTOMER,
			"start_date": add_days(nowdate(), 14),
			"is_signed": is_signed,
			"contract_terms": "Termos de teste B1.",
			"mz_subscription_plan": plan,
			"mz_tenant": tenant,
			"mz_users": users,
		})
		doc.insert(ignore_permissions=True)
		self.track("Contract", doc.name)
		return doc

	# ---- B1: the trigger split -------------------------------------------------

	@patch("ai_saas.saas.contract_lifecycle._setup_subscription")
	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_submit_unsigned_provisions_without_subscription(self, provision, setup_sub):
		doc = self._make_contract(is_signed=0)
		doc.submit()

		provision.assert_called_once_with(doc.name)
		setup_sub.assert_not_called()
		self.assertEqual(frappe.db.get_value("Contract", doc.name, "status"), "Unsigned")

	@patch("ai_saas.saas.contract_lifecycle._setup_subscription")
	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_submit_signed_keeps_manual_path(self, provision, setup_sub):
		doc = self._make_contract(is_signed=1)
		doc.submit()

		provision.assert_called_once_with(doc.name)
		setup_sub.assert_called_once()

	@patch("ai_saas.saas.contract_lifecycle._setup_subscription")
	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_signing_after_submit_creates_subscription_once(self, provision, setup_sub):
		doc = self._make_contract(is_signed=0)
		doc.submit()
		setup_sub.assert_not_called()

		doc.reload()
		doc.is_signed = 1
		doc.save(ignore_permissions=True)

		setup_sub.assert_called_once()

		# A later unrelated update-after-submit must not create a second subscription.
		doc.reload()
		doc.signed_on = frappe.utils.now_datetime()
		doc.save(ignore_permissions=True)
		setup_sub.assert_called_once()

	@patch("ai_saas.saas.contract_lifecycle._setup_subscription")
	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_no_tenant_means_no_provisioning_and_no_phase(self, provision, setup_sub):
		from ai_saas.saas.tenant_lifecycle import account_phase

		doc = self._make_contract(is_signed=0, tenant="")
		doc.submit()

		provision.assert_not_called()
		self.assertEqual(account_phase(doc.name), "")  # no site, no phase: invisible to the engine

	@patch("ai_saas.saas.contract_lifecycle._setup_subscription")
	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_signature_converts_the_opportunity(self, provision, setup_sub):
		"""The Opportunity is the funnel's ledger: the signature closes it as won."""
		opp = frappe.get_doc({
			"doctype": "Opportunity", "opportunity_from": "Customer", "party_name": TEST_CUSTOMER,
			"company": frappe.db.get_single_value("Global Defaults", "default_company"),
			"sales_stage": "Cloud - Account Created",
		}).insert(ignore_permissions=True)
		try:
			doc = self._make_contract(is_signed=0)
			doc.submit()
			self.assertEqual(frappe.db.get_value("Opportunity", opp.name, "sales_stage"), "Cloud - Account Created")
			doc.reload()
			doc.is_signed = 1
			doc.save(ignore_permissions=True)
			o = frappe.db.get_value("Opportunity", opp.name, ["sales_stage", "status", "mz_stage_since"], as_dict=True)
			self.assertEqual((o.sales_stage, o.status), ("Cloud - Activated", "Converted"))
			self.assertTrue(o.mz_stage_since)
		finally:
			frappe.delete_doc("Opportunity", opp.name, force=True, ignore_permissions=True)

	# ---- B3: the plan guard ----------------------------------------------------

	@patch("ai_saas.saas.contract_lifecycle._setup_subscription")
	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_plan_editable_until_subscription_exists(self, provision, setup_sub):
		doc = self._make_contract(is_signed=0)
		doc.submit()

		doc.reload()
		doc.mz_subscription_plan = OTHER_PLAN
		doc.save(ignore_permissions=True)  # no subscription linked -> allowed
		self.assertEqual(
			frappe.db.get_value("Contract", doc.name, "mz_subscription_plan"), OTHER_PLAN
		)

		from ai_saas.saas.contract_lifecycle import _get_company

		sub = frappe.get_doc({
			"doctype": "Subscription", "party_type": "Customer", "party": TEST_CUSTOMER, "company": _get_company(),
			"start_date": add_days(nowdate(), 30), "generate_invoice_at": "Beginning of the current subscription period",
			"plans": [{"plan": TEST_PLAN, "qty": 1}],
		}).insert(ignore_permissions=True)
		self.track("Subscription", sub.name)
		frappe.db.set_value("Contract", doc.name, "mz_linked_subscription", sub.name)

		doc.reload()
		doc.mz_subscription_plan = TEST_PLAN
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)

	# ---- Per-user pricing: Contract.mz_users -> Subscription qty -----------------
	# First user included: N seats bill N-1 plan costs, floor 1 (_billed_qty).
	# _setup_subscription runs for real here (billing start is 14 days out, so no
	# invoice is generated); only provisioning is patched.

	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_subscription_qty_from_mz_users(self, provision):
		doc = self._make_contract(is_signed=1, users=5)
		doc.submit()
		sub = frappe.db.get_value("Contract", doc.name, "mz_linked_subscription")
		self.assertTrue(sub)
		self.assertEqual(frappe.db.get_value("Subscription Plan Detail", {"parent": sub}, "qty"), 4)

	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_minimum_seats_bill_one_plan_cost(self, provision):
		"""The entry price: 2 seats (the self-service minimum) = qty 1 = one plan cost."""
		doc = self._make_contract(is_signed=1, users=2)
		doc.submit()
		sub = frappe.db.get_value("Contract", doc.name, "mz_linked_subscription")
		self.assertEqual(frappe.db.get_value("Subscription Plan Detail", {"parent": sub}, "qty"), 1)

	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_subscription_qty_defaults_to_one(self, provision):
		"""Empty seats (desk/legacy path) bill 1 — the pre-seats flat behaviour — and
		the contract is normalised to the entitlement that buys (billed + 1)."""
		doc = self._make_contract(is_signed=1)
		doc.submit()
		sub = frappe.db.get_value("Contract", doc.name, "mz_linked_subscription")
		self.assertEqual(frappe.db.get_value("Subscription Plan Detail", {"parent": sub}, "qty"), 1)
		self.assertEqual(frappe.db.get_value("Contract", doc.name, "mz_users"), 2)

	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_seat_change_updates_linked_subscription(self, provision):
		doc = self._make_contract(is_signed=1, users=5)
		doc.submit()
		sub = frappe.db.get_value("Contract", doc.name, "mz_linked_subscription")

		doc.reload()
		doc.mz_users = 8
		doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("Subscription Plan Detail", {"parent": sub}, "qty"), 7)
		# The same, single subscription — a seats-only save never re-runs the signature path.
		self.assertEqual(frappe.db.get_value("Contract", doc.name, "mz_linked_subscription"), sub)
		self.assertEqual(frappe.db.count("Subscription", {"party": TEST_CUSTOMER}), 1)

	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_seat_change_to_zero_refused_on_live_subscription(self, provision):
		doc = self._make_contract(is_signed=1, users=3)
		doc.submit()
		sub = frappe.db.get_value("Contract", doc.name, "mz_linked_subscription")

		doc.reload()
		doc.mz_users = 0
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("Subscription Plan Detail", {"parent": sub}, "qty"), 2)

	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_plan_and_seat_change_together_hits_b3(self, provision):
		"""B3 runs first and rolls the whole save back — the seats stay untouched too."""
		doc = self._make_contract(is_signed=1, users=5)
		doc.submit()
		sub = frappe.db.get_value("Contract", doc.name, "mz_linked_subscription")

		doc.reload()
		doc.mz_subscription_plan = OTHER_PLAN
		doc.mz_users = 9
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("Subscription Plan Detail", {"parent": sub}, "qty"), 4)

	@patch("ai_saas.saas.contract_lifecycle._setup_subscription")
	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_seat_change_on_unsigned_contract_is_free(self, provision, setup_sub):
		doc = self._make_contract(is_signed=0, users=3)
		doc.submit()

		doc.reload()
		doc.mz_users = 7
		doc.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("Contract", doc.name, "mz_users"), 7)
		setup_sub.assert_not_called()

	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_backfill_patch_stamps_seats_from_subscription(self, provision):
		from ai_saas.patches.v1 import set_contract_users_from_subscription

		doc = self._make_contract(is_signed=1, users=4)  # 4 seats -> billed qty 3
		doc.submit()
		frappe.db.set_value("Contract", doc.name, "mz_users", 0, update_modified=False)
		unsigned = self._make_contract(is_signed=0)
		unsigned.submit()

		set_contract_users_from_subscription.execute()
		# Billing-preserving roundtrip: seats = qty + 1 (the included first user).
		self.assertEqual(frappe.db.get_value("Contract", doc.name, "mz_users"), 4)
		# No linked subscription -> left alone (the /activar question decides later).
		self.assertEqual(frappe.db.get_value("Contract", unsigned.name, "mz_users"), 0)

	# ---- B4: the contract template ---------------------------------------------

	def test_contract_template_exists_and_renders(self):
		from erpnext.crm.doctype.contract_template.contract_template import get_contract_template

		self.assertTrue(frappe.db.exists("Contract Template", "MozEconomia Cloud"))
		result = get_contract_template(
			"MozEconomia Cloud",
			{
				"party_name": TEST_CUSTOMER,
				"mz_subscription_plan": TEST_PLAN,
				"mz_tenant_url": f"{TEST_SLUG}.erp.mozeconomia.co.mz",
				"start_date": add_days(nowdate(), 14),
			},
		)
		terms = result["contract_terms"] if isinstance(result, dict) else result.contract_terms
		self.assertIn(TEST_CUSTOMER, terms)
		self.assertIn(TEST_PLAN, terms)
		self.assertNotIn("{{", terms)  # every placeholder rendered

	# ---- B5: primary contact resolution ----------------------------------------

	def test_primary_contact_resolved_via_dynamic_link(self):
		from ai_saas.saas.contract_lifecycle import _ensure_customer_primary_contact

		contact = frappe.get_doc({
			"doctype": "Contact",
			"first_name": "Teste B5",
			"is_primary_contact": 1,
			"email_ids": [{"email_id": "b5@example.com", "is_primary": 1}],
			"links": [{"link_doctype": "Customer", "link_name": TEST_CUSTOMER}],
		})
		contact.insert(ignore_permissions=True)
		self.track("Contact", contact.name)

		_ensure_customer_primary_contact(frappe._dict(party_name=TEST_CUSTOMER))

		self.assertEqual(
			frappe.db.get_value("Customer", TEST_CUSTOMER, "customer_primary_contact"),
			contact.name,
		)
		self.assertEqual(
			frappe.db.get_value("Customer", TEST_CUSTOMER, "email_id"), "b5@example.com"
		)

		# Never overwrite an answer already given.
		other = frappe.get_doc({
			"doctype": "Contact",
			"first_name": "Teste B5 Segundo",
			"links": [{"link_doctype": "Customer", "link_name": TEST_CUSTOMER}],
		})
		other.insert(ignore_permissions=True)
		self.track("Contact", other.name)
		_ensure_customer_primary_contact(frappe._dict(party_name=TEST_CUSTOMER))
		self.assertEqual(
			frappe.db.get_value("Customer", TEST_CUSTOMER, "customer_primary_contact"),
			contact.name,
		)
