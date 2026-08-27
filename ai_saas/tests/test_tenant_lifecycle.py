"""Tests for the tenant lifecycle engine (docs/sales-funnel-implementation.md, F1-F6).

The bench subprocess (_run_cmd) is always patched — no site is ever touched.
What is exercised: the dry-run fail-safe, the phase-field contract (empty =
untouchable), the state transitions on the provisioning record, the archive
gates, and the review queue's controller dispatch.

Known bench pattern: doc.submit() commits, so tearDown cleans up explicitly.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

from ai_saas.saas import tenant_lifecycle

TEST_CUSTOMER = "_Test Cliente AI SaaS F"
TEST_PLAN = "Premium Mensal - MozEconomia Cloud"
TEST_SLUG = "f2-teste"


class TestTenantLifecycle(FrappeTestCase):
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
		self._contracts = []
		self._provs = []
		self._reviews = []
		frappe.db.commit()

	def tearDown(self):
		for name in self._reviews:
			frappe.delete_doc("MZ Overdue Review", name, force=True, ignore_missing=True, ignore_permissions=True)
		for name in self._provs:
			frappe.delete_doc("MZ Tenant Provisioning", name, force=True, ignore_missing=True, ignore_permissions=True)
		for name in self._contracts:
			if frappe.db.exists("Contract", name):
				doc = frappe.get_doc("Contract", name)
				if doc.docstatus == 1:
					doc.cancel()
				frappe.delete_doc("Contract", doc.name, force=True, ignore_permissions=True)
		if frappe.db.exists("Customer", TEST_CUSTOMER):
			frappe.delete_doc("Customer", TEST_CUSTOMER, force=True, ignore_permissions=True)
		frappe.db.commit()

	def _make_trial(self, start_date, slug=TEST_SLUG):
		"""Submitted unsigned cloud contract (provisioning patched) + its prov record."""
		with patch("ai_saas.saas.provisioning.provision_tenant"):
			doc = frappe.get_doc({
				"doctype": "Contract",
				"party_type": "Customer",
				"party_name": TEST_CUSTOMER,
				"start_date": start_date,
				"contract_terms": "Termos de teste F.",
				"mz_subscription_plan": TEST_PLAN,
				"mz_tenant": slug,
			})
			doc.insert(ignore_permissions=True)
			self._contracts.append(doc.name)
			doc.submit()

		prov = frappe.get_doc({
			"doctype": "MZ Tenant Provisioning",
			"contract": doc.name,
			"tenant_slug": slug,
			"site_name": f"{slug}.erp.mozeconomia.co.mz",
			"status": "Active",
		})
		prov.insert(ignore_permissions=True)
		self._provs.append(prov.name)
		frappe.db.commit()
		return doc, prov

	# ---- F1: the fail-safe defaults ---------------------------------------------

	def test_settings_default_to_nothing_armed(self):
		"""An unsaved Single returns None for every field: nothing may execute until a box is ticked."""
		with patch("frappe.db.get_single_value", return_value=None):
			s = tenant_lifecycle.get_settings()
		self.assertEqual((s.overdue_days_to_suspend, s.grace_days_to_archive, s.auto_suspend, s.auto_archive), (33, 30, 0, 0))
		armed = lambda doctype, field: 1 if field in ("auto_suspend", "auto_archive") else None
		with patch("frappe.db.get_single_value", side_effect=armed):
			s = tenant_lifecycle.get_settings()
		self.assertEqual((s.auto_suspend, s.auto_archive), (1, 1))

	@patch("ai_saas.saas.tenant_lifecycle.run_cmd")
	def test_reactivate_billing_customer_needs_settled_debt_or_force(self, run_cmd):
		doc, prov = self._make_trial(start_date=add_days(nowdate(), -60))
		frappe.db.set_value("Contract", doc.name, {"is_signed": 1, "mz_account_phase": "Active"}, update_modified=False)
		tenant_lifecycle.suspend(doc.name)
		with patch("ai_saas.saas.tenant_lifecycle._has_overdue_invoice", return_value=True):
			with self.assertRaises(frappe.ValidationError):
				tenant_lifecycle.reactivate(doc.name)
			tenant_lifecycle.reactivate(doc.name, force=True)
		self.assertEqual(frappe.db.get_value("Contract", doc.name, "mz_account_phase"), "Active")

	# ---- F3: the engine, in dry-run ---------------------------------------------

	@patch("ai_saas.saas.tenant_lifecycle.run_cmd")
	def test_unarmed_engine_names_only_phased_contracts(self, run_cmd):
		expired, _ = self._make_trial(start_date=add_days(nowdate(), -3))

		# A commercial contract with the same dates but no phase (no tenant) —
		# must never be named by the engine.
		with patch("ai_saas.saas.provisioning.provision_tenant"):
			plain = frappe.get_doc({
				"doctype": "Contract",
				"party_type": "Customer",
				"party_name": TEST_CUSTOMER,
				"start_date": add_days(nowdate(), -3),
				"contract_terms": "Termos de teste F (sem cloud).",
				"mz_subscription_plan": TEST_PLAN,
			})
			plain.insert(ignore_permissions=True)
			self._contracts.append(plain.name)
			plain.submit()

		real = tenant_lifecycle.get_settings()
		real.update({"auto_suspend": 0, "auto_archive": 0, "ops_alert_recipients": []})
		with patch.object(tenant_lifecycle, "get_settings", return_value=real):
			actions = tenant_lifecycle.process_lifecycle()

		named = [a for a in actions if expired.name in a]
		self.assertEqual(len(named), 1)
		self.assertTrue(named[0].startswith("[só observação]"))
		self.assertFalse(any(plain.name in a for a in actions))
		# not armed: executed nothing
		run_cmd.assert_not_called()
		self.assertEqual(
			frappe.db.get_value("Contract", expired.name, "mz_account_phase"), "Trial"
		)

	# ---- F2: suspend / reactivate / archive gates -------------------------------

	@patch("ai_saas.saas.tenant_lifecycle.run_cmd")
	def test_suspend_and_reactivate_roundtrip(self, run_cmd):
		doc, prov = self._make_trial(start_date=add_days(nowdate(), -1))

		with patch("ai_saas.saas.tenant_lifecycle.send_lifecycle_email") as mail:
			tenant_lifecycle.suspend(doc.name, reason="teste", cause="trial")
		mail.assert_called_once_with("suspended", doc.name, cause="trial", invoice="")
		prov.reload()
		self.assertEqual(prov.status, "Suspended")
		self.assertTrue(prov.suspended_on)
		self.assertEqual(frappe.db.get_value("Contract", doc.name, "mz_account_phase"), "Suspended")
		self.assertIn("set-maintenance-mode", run_cmd.call_args[0][0])

		# Idempotent: a second suspend is a no-op.
		calls = run_cmd.call_count
		tenant_lifecycle.suspend(doc.name)
		self.assertEqual(run_cmd.call_count, calls)

		# Reactivating an expired unsigned trial without a new date must refuse.
		with self.assertRaises(frappe.ValidationError):
			tenant_lifecycle.reactivate(doc.name)

		new_end = add_days(nowdate(), 7)
		with patch("ai_saas.saas.tenant_lifecycle.send_lifecycle_email") as mail:
			tenant_lifecycle.reactivate(doc.name, new_start_date=new_end)
		mail.assert_called_once_with("reactivated", doc.name, new_trial_end=new_end)
		prov.reload()
		self.assertEqual(prov.status, "Active")
		self.assertFalse(prov.suspended_on)
		self.assertEqual(frappe.db.get_value("Contract", doc.name, "mz_account_phase"), "Trial")
		self.assertEqual(str(frappe.db.get_value("Contract", doc.name, "start_date")), str(new_end))

	@patch("ai_saas.saas.tenant_lifecycle.run_cmd")
	def test_archive_refuses_unsuspended(self, run_cmd):
		doc, prov = self._make_trial(start_date=add_days(nowdate(), 7))
		with self.assertRaises(frappe.ValidationError):
			tenant_lifecycle.archive(doc.name)
		run_cmd.assert_not_called()

	@patch("ai_saas.saas.tenant_lifecycle.run_cmd")
	def test_engine_survives_a_contract_without_a_site(self, run_cmd):
		"""Rule 1 selects an expired trial whose provisioning never happened; the run
		must record the failure and still process the next contract."""
		broken, prov_b = self._make_trial(start_date=add_days(nowdate(), -3), slug="f3-broken")
		frappe.delete_doc("MZ Tenant Provisioning", prov_b.name, force=True, ignore_permissions=True)
		self._provs.remove(prov_b.name)
		good, _ = self._make_trial(start_date=add_days(nowdate(), -3), slug="f3-good")

		real = tenant_lifecycle.get_settings()
		real.update({"auto_suspend": 1, "auto_archive": 0, "ops_alert_recipients": []})
		with patch.object(tenant_lifecycle, "get_settings", return_value=real):
			actions = tenant_lifecycle.process_lifecycle()

		self.assertTrue(any(a.startswith("FALHOU " + broken.name) for a in actions))
		self.assertEqual(frappe.db.get_value("Contract", good.name, "mz_account_phase"), "Suspended")
		self.assertEqual(frappe.db.get_value("Contract", broken.name, "mz_account_phase"), "Trial")

	@patch("ai_saas.saas.tenant_lifecycle.run_cmd")
	def test_archive_after_grace(self, run_cmd):
		doc, prov = self._make_trial(start_date=add_days(nowdate(), -40))
		tenant_lifecycle.suspend(doc.name)
		frappe.db.set_value("MZ Tenant Provisioning", prov.name, "suspended_on",
		                    frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-31))
		real = tenant_lifecycle.get_settings()
		real.update({"auto_suspend": 1, "auto_archive": 1, "ops_alert_recipients": [], "grace_days_to_archive": 30})
		with patch.object(tenant_lifecycle, "get_settings", return_value=real), \
		     patch("ai_saas.saas.tenant_lifecycle._has_recent_backup", return_value=True), \
		     patch("ai_saas.saas.tenant_lifecycle.os.path.isdir", return_value=True), \
		     patch("ai_saas.saas.tenant_lifecycle.get_db_root_password", return_value="x"):
			actions = tenant_lifecycle.process_lifecycle()

		self.assertTrue(any(a.startswith("arquivar") and doc.name in a for a in actions))
		prov.reload()
		self.assertEqual(prov.status, "Archived")
		# With only suspension armed, an archive candidate is reported, never executed.
		doc2, prov2 = self._make_trial(start_date=add_days(nowdate(), -40), slug="f3-obs")
		tenant_lifecycle.suspend(doc2.name)
		frappe.db.set_value("MZ Tenant Provisioning", prov2.name, "suspended_on",
		                    frappe.utils.add_to_date(frappe.utils.now_datetime(), days=-31))
		real.update({"auto_archive": 0})
		with patch.object(tenant_lifecycle, "get_settings", return_value=real):
			actions = tenant_lifecycle.process_lifecycle()
		self.assertTrue(any(a.startswith("[só observação] arquivar") and doc2.name in a for a in actions))
		self.assertEqual(frappe.db.get_value("MZ Tenant Provisioning", prov2.name, "status"), "Suspended")
		self.assertTrue(prov.backup_path.endswith(prov.site_name))
		self.assertEqual(frappe.db.get_value("Contract", doc.name, "mz_account_phase"), "Closed")
		cmds = [c.args[0] for c in run_cmd.call_args_list]
		self.assertTrue(any("backup" in c for c in cmds) and any("drop-site" in c for c in cmds))
		# Archive is final: a second run does nothing to it.
		with patch.object(tenant_lifecycle, "get_settings", return_value=real):
			tenant_lifecycle.process_lifecycle()
		self.assertEqual(frappe.db.get_value("MZ Tenant Provisioning", prov.name, "status"), "Archived")

	def test_suspend_refuses_unprovisioned_site(self):
		doc, prov = self._make_trial(start_date=add_days(nowdate(), 3))
		frappe.db.set_value("MZ Tenant Provisioning", prov.name, "status", "Queued")
		with self.assertRaises(frappe.ValidationError):
			tenant_lifecycle.suspend(doc.name)

	# ---- F4: the review queue executes ------------------------------------------

	def test_review_queue_dispatches_suspend(self):
		doc, prov = self._make_trial(start_date=add_days(nowdate(), 7))
		review = frappe.get_doc({
			"doctype": "MZ Overdue Review",
			"customer": TEST_CUSTOMER,
			"contract": doc.name,
			"review_status": "Pending Review",
		})
		review.insert(ignore_permissions=True)
		self._reviews.append(review.name)

		with patch("ai_saas.saas.tenant_lifecycle.suspend") as mock_suspend:
			review.review_status = "Suspend"
			review.save(ignore_permissions=True)
		mock_suspend.assert_called_once()
		self.assertEqual(mock_suspend.call_args[0][0], doc.name)

		# Saving again without a state change must not re-dispatch.
		with patch("ai_saas.saas.tenant_lifecycle.suspend") as mock_suspend:
			review.reload()
			review.notes = "sem transição"
			review.save(ignore_permissions=True)
		mock_suspend.assert_not_called()


class TestLifecycleMail(TestTenantLifecycle):
	"""The three customer emails: rendered from Email Templates with the contract's context."""

	def _ctx_and_send(self, kind, doc, **extra):
		from ai_saas.saas import lifecycle_mail

		frappe.db.set_value("Contract", doc.name, "contact_email", "cliente@example.com", update_modified=False)
		with patch("ai_saas.saas.lifecycle_mail.frappe.sendmail") as sendmail:
			self.assertTrue(lifecycle_mail.send_lifecycle_email(kind, doc.name, **extra))
		return sendmail.call_args.kwargs

	def test_templates_exist_and_suspended_trial_sells_activation(self):
		from ai_saas.install import LIFECYCLE_EMAIL_TEMPLATES, ensure_email_templates
		ensure_email_templates()
		for name in LIFECYCLE_EMAIL_TEMPLATES:
			self.assertTrue(frappe.db.exists("Email Template", name), name)

		doc, prov = self._make_trial(start_date=add_days(nowdate(), -1))
		mail = self._ctx_and_send("suspended", doc, cause="trial")
		self.assertEqual(mail["recipients"], ["cliente@example.com"])
		self.assertIn(TEST_CUSTOMER, mail["subject"])
		self.assertIn("período experimental terminou", mail["message"])
		self.assertIn("/activar?contract=" + doc.name, mail["message"])
		self.assertNotIn("falta de pagamento", mail["message"])
		self.assertIn(prov.site_name, mail["message"])
		self.assertNotIn("{{", mail["message"])

	def test_suspended_for_overdue_names_the_invoice(self):
		doc, prov = self._make_trial(start_date=add_days(nowdate(), -1))
		mail = self._ctx_and_send("suspended", doc, cause="overdue", invoice="ACC-SINV-2026-99999")
		self.assertIn("ACC-SINV-2026-99999", mail["message"])
		self.assertIn("regularize a factura", mail["message"])
		self.assertNotIn("/activar", mail["message"])

	def test_reactivated_and_archived(self):
		doc, prov = self._make_trial(start_date=add_days(nowdate(), -1))
		new_end = add_days(nowdate(), 7)
		mail = self._ctx_and_send("reactivated", doc, new_trial_end=new_end)
		self.assertIn("está de volta", mail["subject"])
		self.assertIn(frappe.utils.formatdate(new_end), mail["message"])
		self.assertIn(f"https://{prov.site_name}", mail["message"])

		mail = self._ctx_and_send("archived", doc)
		self.assertIn("arquivada", mail["subject"])
		self.assertIn("cópia de segurança completa", mail["message"])
		self.assertEqual(mail["reference_name"], doc.name)

	def test_activated_states_plan_and_billing_start(self):
		doc, prov = self._make_trial(start_date=add_days(nowdate(), 3))
		frappe.db.set_value("Contract", doc.name, {"is_signed": 1, "mz_billing_start": add_days(nowdate(), 3)}, update_modified=False)
		mail = self._ctx_and_send("activated", doc)
		self.assertIn("está activa", mail["subject"])
		self.assertIn(TEST_PLAN, mail["message"])
		self.assertIn(frappe.utils.formatdate(add_days(nowdate(), 3)), mail["message"])
		self.assertIn(f"https://{prov.site_name}", mail["message"])

	def test_no_recipient_is_logged_not_raised(self):
		from ai_saas.saas import lifecycle_mail
		doc, prov = self._make_trial(start_date=add_days(nowdate(), -1))
		frappe.db.set_value("Contract", doc.name, "contact_email", "", update_modified=False)
		frappe.db.set_value("Customer", TEST_CUSTOMER, "email_id", "", update_modified=False)
		with patch("ai_saas.saas.lifecycle_mail.frappe.sendmail") as sendmail:
			self.assertFalse(lifecycle_mail.send_lifecycle_email("archived", doc.name))
		sendmail.assert_not_called()
