"""Tests for E1 (activation = signing through the save path), E2 (billing start)
and E3 (mz_billing_start stamping). Subscription creation is real for the E2
stamping test (no invoice: billing start is in the future); provisioning is patched."""

from unittest.mock import patch

import frappe
from frappe.utils import add_days, getdate, nowdate

from ai_saas.saas import activation
from ai_saas.saas.contract_lifecycle import compute_billing_start
from ai_saas.tests.helpers import FunnelTestCase

TEST_CUSTOMER = "_Test Cliente AI SaaS E"
TEST_PLAN = "Premium Mensal - MozEconomia Cloud"
OTHER_PLAN = "Premium Anual - MozEconomia Cloud"
TEST_SLUG = "e1-teste"


class TestActivation(FunnelTestCase):
	CUSTOMER = TEST_CUSTOMER

	def tearDown(self):
		# activation creates a Billing Address through the Customer's Dynamic Links
		for a in frappe.get_all("Dynamic Link", {"link_doctype": "Customer", "link_name": TEST_CUSTOMER, "parenttype": "Address"}, pluck="parent"):
			frappe.delete_doc("Address", a, force=True, ignore_missing=True, ignore_permissions=True)
		super().tearDown()

	def _make_trial(self, start_date, prov_status="Active"):
		with patch("ai_saas.saas.provisioning.provision_tenant"):
			doc = frappe.get_doc({
				"doctype": "Contract",
				"party_type": "Customer",
				"party_name": TEST_CUSTOMER,
				"start_date": start_date,
				"contract_terms": "Termos de teste E.",
				"mz_subscription_plan": TEST_PLAN,
				"mz_tenant": TEST_SLUG,
			}).insert(ignore_permissions=True)
			self.track("Contract", doc.name)
			doc.submit()
		frappe.get_doc({
			"doctype": "MZ Tenant Provisioning", "contract": doc.name, "tenant_slug": TEST_SLUG,
			"site_name": f"{TEST_SLUG}.erp.mozeconomia.co.mz", "status": prov_status,
		}).insert(ignore_permissions=True)
		return doc

	def test_activation_refuses_contracts_without_a_usable_site(self):
		for prov_status in ("Archived", "Failed", "Queued"):
			doc = self._make_trial(add_days(nowdate(), 14), prov_status=prov_status)
			with self.assertRaises(frappe.ValidationError):
				activation._activate(doc.name, activation.get_activation_token(doc.name), accept_terms=1)
			self.assertEqual(frappe.db.get_value("Contract", doc.name, "is_signed"), 0)

	# ---- E2: the rule ------------------------------------------------------------

	def test_billing_start_is_the_later_date(self):
		future = add_days(nowdate(), 10)
		self.assertEqual(compute_billing_start(frappe._dict(start_date=future)), getdate(future))
		self.assertEqual(compute_billing_start(frappe._dict(start_date=add_days(nowdate(), -10))), getdate(nowdate()))
		self.assertEqual(compute_billing_start(frappe._dict(start_date=nowdate())), getdate(nowdate()))

	# ---- E1: token + context -------------------------------------------------------

	def test_token_gates_the_context(self):
		doc = self._make_trial(add_days(nowdate(), 14))
		url = activation.get_activation_url(doc.name)
		self.assertIn(f"contract={doc.name}", url)
		self.assertIn("token=", url)

		bad = activation.get_activation_context(doc.name, "0000000000000000")
		self.assertFalse(bad.valid)

		ctx = activation.get_activation_context(doc.name, activation.get_activation_token(doc.name))
		self.assertTrue(ctx.valid)
		self.assertFalse(ctx.already_signed)
		self.assertEqual(ctx.contract.name, doc.name)
		self.assertTrue(ctx.plans)

	def test_activate_refuses_bad_token_and_unaccepted_terms(self):
		doc = self._make_trial(add_days(nowdate(), 14))
		with self.assertRaises(frappe.PermissionError):
			activation._activate(doc.name, "0000000000000000", accept_terms=1)
		with self.assertRaises(frappe.ValidationError):
			activation._activate(doc.name, activation.get_activation_token(doc.name), accept_terms=0)
		self.assertEqual(frappe.db.get_value("Contract", doc.name, "is_signed"), 0)

	# ---- E1 + E2 + E3: the signature through the save path -------------------------

	def test_activation_signs_creates_subscription_and_stamps_billing_start(self):
		future = add_days(nowdate(), 14)
		doc = self._make_trial(future)
		token = activation.get_activation_token(doc.name)

		with patch("ai_saas.saas.provisioning.provision_tenant"), \
		     patch("ai_saas.saas.activation.send_lifecycle_email") as mail:
			result = activation._activate(
				doc.name, token, plan=OTHER_PLAN, tax_id="123456789",
				address_line1="Av. 25 de Setembro, 100", city="Maputo", accept_terms=1,
			)
		self.assertTrue(result["ok"])
		mail.assert_called_once_with("activated", doc.name)  # the confirmation every activator gets

		c = frappe.db.get_value(
			"Contract", doc.name,
			["is_signed", "status", "mz_subscription_plan", "mz_linked_subscription", "mz_billing_start", "signed_on"],
			as_dict=True,
		)
		self.assertEqual(c.is_signed, 1)
		self.assertEqual(c.status, "Active")                 # native recompute ran -> save path
		from ai_saas.saas.tenant_lifecycle import account_phase
		self.assertEqual(account_phase(doc.name), "Active")  # derived: signed + site up
		self.assertEqual(c.mz_subscription_plan, OTHER_PLAN) # plan corrected at signing (B3)
		self.assertTrue(c.mz_linked_subscription)            # Subscription created
		self.assertEqual(str(c.mz_billing_start), str(future))  # E2/E3: later of the two dates
		self.assertTrue(c.signed_on)
		self.assertEqual(
			str(frappe.db.get_value("Subscription", c.mz_linked_subscription, "start_date")), str(future)
		)
		# Verification 6: E2 depends on ERPNext generating the first invoice only on an exact
		# date match. If an upgrade relaxes this, E2's "issue today" shortcut becomes a duplicate.
		sub = frappe.get_doc("Subscription", c.mz_linked_subscription)
		self.assertTrue(sub.can_generate_new_invoice(future))
		self.assertFalse(sub.can_generate_new_invoice(add_days(future, 1)))
		self.assertFalse(sub.can_generate_new_invoice(add_days(future, -1)))
		self.assertEqual(frappe.db.get_value("Customer", TEST_CUSTOMER, "tax_id"), "123456789")
		addr = activation._get_billing_address(TEST_CUSTOMER)
		self.assertEqual(addr.address_type, "Billing")
		self.assertEqual(addr.city, "Maputo")

		# Idempotent: a second click is a no-op.
		again = activation._activate(doc.name, token, accept_terms=1)
		self.assertTrue(again.get("already_signed"))
		self.assertEqual(frappe.db.count("Subscription", {"party": TEST_CUSTOMER}), 1)

	def test_billing_address_update_path_and_nuit_validation(self):
		doc = self._make_trial(add_days(nowdate(), 14))
		token = activation.get_activation_token(doc.name)
		frappe.get_doc({"doctype": "Address", "address_title": TEST_CUSTOMER, "address_type": "Shipping",
		                "address_line1": "Maputo", "city": "Maputo", "country": "Mozambique",
		                "links": [{"link_doctype": "Customer", "link_name": TEST_CUSTOMER}]}).insert(ignore_permissions=True)
		with self.assertRaises(frappe.ValidationError):
			activation._activate(doc.name, token, tax_id="12", accept_terms=1)
		with patch("ai_saas.saas.contract_lifecycle._setup_subscription"), patch("ai_saas.saas.provisioning.provision_tenant"):
			activation._activate(doc.name, token, tax_id="400 123 456", address_line1="Rua Nova, 1", city="Beira", accept_terms=1)
		addr = activation._get_billing_address(TEST_CUSTOMER)
		self.assertEqual((addr.address_type, addr.address_line1, addr.city), ("Billing", "Rua Nova, 1", "Beira"))
		self.assertEqual(frappe.db.count("Address", {"address_title": TEST_CUSTOMER}), 1)   # updated, not duplicated
		self.assertEqual(frappe.db.get_value("Customer", TEST_CUSTOMER, "tax_id"), "400123456")

	def test_activation_while_suspended_reactivates(self):
		doc = self._make_trial(add_days(nowdate(), -3), prov_status="Suspended")
		token = activation.get_activation_token(doc.name)
		with patch("ai_saas.saas.contract_lifecycle._setup_subscription"), \
		     patch("ai_saas.saas.provisioning.provision_tenant"), \
		     patch("ai_saas.saas.tenant_lifecycle.reactivate") as react:
			activation._activate(doc.name, token, accept_terms=1)
		react.assert_called_once()
		self.assertEqual(react.call_args[0][0], doc.name)
		self.assertEqual(frappe.db.get_value("Contract", doc.name, "is_signed"), 1)
