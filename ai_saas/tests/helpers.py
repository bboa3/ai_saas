"""Shared fixtures for the funnel tests: every suite used to skip on a fresh site
because the MozEconomia Cloud plans were absent — which made a fresh site silently
"green" with zero tests run. ensure_test_plan() creates what is missing instead."""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

TEST_PLAN = "Premium Mensal - MozEconomia Cloud"
OTHER_PLAN = "Premium Anual - MozEconomia Cloud"
BASIC_PLAN = "_Test Basico - MozEconomia Cloud"
TEST_ITEM = "_Test MZ Cloud Plan Item"
_ITEM = TEST_ITEM


def _root(doctype, parent_field):
	return (
		frappe.db.get_value(doctype, {"is_group": 1, parent_field: ("in", ["", None])}, "name")
		or frappe.db.get_value(doctype, {"is_group": 1}, "name", order_by="lft asc")
	)


def ensure_test_plan():
	"""Create the two plans self-service offers when the site has none, with a test Item."""
	if all(frappe.db.exists("Subscription Plan", n) for n in (TEST_PLAN, OTHER_PLAN, BASIC_PLAN)):
		return
	if not frappe.db.exists("Item", _ITEM):
		frappe.get_doc({
			"doctype": "Item", "item_code": _ITEM, "item_name": _ITEM, "is_stock_item": 0,
			"item_group": _root("Item Group", "parent_item_group"),
			"stock_uom": frappe.db.get_value("UOM", {}, "name"),
		}).insert(ignore_permissions=True)
	for name, cost, interval, cloud in ((TEST_PLAN, 2999, "Month", 1), (OTHER_PLAN, 29990, "Year", 1), (BASIC_PLAN, 999, "Month", 0)):
		if not frappe.db.exists("Subscription Plan", name):
			frappe.get_doc({
				"doctype": "Subscription Plan", "plan_name": name, "item": _ITEM, "price_determination": "Fixed Rate",
				"cost": cost, "currency": "MZN", "billing_interval": interval, "billing_interval_count": 1,
				"mz_cloud_plan": cloud,  # the basic test plan must never show on /registo of a real site
			}).insert(ignore_permissions=True)
	frappe.db.commit()


def make_test_customer(name):
	if not frappe.db.exists("Customer", name):
		frappe.get_doc({
			"doctype": "Customer", "customer_name": name, "customer_type": "Company",
			"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
			"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
		}).insert(ignore_permissions=True)
	return name


def cleanup_contract(name):
	"""Cancel-if-submitted then delete; tolerate a missing record."""
	if not frappe.db.exists("Contract", name):
		return
	frappe.db.set_value("Contract", name, "mz_linked_subscription", None, update_modified=False)
	doc = frappe.get_doc("Contract", name)
	if doc.docstatus == 1:
		doc.cancel()
	frappe.delete_doc("Contract", name, force=True, ignore_permissions=True)


def before_tests():
	"""Run once at the start of `bench run-tests --app ai_saas` (hooks.before_tests).

	Tests insert documents whose Notifications queue real mail — a signup fires
	"Dia 0", an Opportunity fires the nurture set — and muting only stops the
	*sending*, not the queuing. So the run is muted (nothing leaves this process)
	and the previous run's queued messages to example.com are purged, which keeps
	the site's Email Queue from filling with test mail that would go out the day
	someone enables the scheduler.
	"""
	frappe.flags.mute_emails = True
	purge_test_emails()
	purge_orphan_provisioning()


def purge_orphan_provisioning():
	"""Provisioning rows whose contract is gone. A deleted contract reverts the naming
	series, so a later test's contract can get the same name — and inherit a stale site
	record, which the lifecycle engine's join would then count twice."""
	orphans = frappe.db.sql_list("""
		SELECT p.name FROM `tabMZ Tenant Provisioning` p
		LEFT JOIN `tabContract` c ON c.name = p.contract
		WHERE c.name IS NULL
	""")
	for name in orphans:
		frappe.delete_doc("MZ Tenant Provisioning", name, force=True, ignore_permissions=True)
	if orphans:
		frappe.db.commit()


def purge_test_emails():
	"""Delete queued messages addressed only to example.com — test recipients, never
	a real one. Real addresses in the queue are left exactly as they are."""
	names = frappe.db.sql_list("""
		SELECT DISTINCT q.name FROM `tabEmail Queue` q
		WHERE q.status IN ('Not Sent', 'Error')
		  AND NOT EXISTS (SELECT 1 FROM `tabEmail Queue Recipient` r
		                  WHERE r.parent = q.name AND r.recipient NOT LIKE %s)
		  AND EXISTS (SELECT 1 FROM `tabEmail Queue Recipient` r WHERE r.parent = q.name)
	""", "%@example.com")
	if not names:
		return
	frappe.db.delete("Email Queue Recipient", {"parent": ("in", names)})
	frappe.db.delete("Email Queue", {"name": ("in", names)})
	frappe.db.commit()


class FunnelTestCase(FrappeTestCase):
	"""Shared scaffolding for the funnel suites (workstream B, 2026-08-31).

	One test Customer per suite (set CUSTOMER, optionally CUSTOMER_EMAIL), a
	`track()` registry, and a teardown that survives doc.submit()'s commits:
	tracked documents are removed newest-first with their dependants (a Contract
	takes its provisioning rows, linked Subscription and ToDos with it), then the
	Customer, then one commit. Tests that submit documents MUST track them —
	rollback cannot undo what submit committed.
	"""

	CUSTOMER = "_Test Funnel Customer"
	CUSTOMER_EMAIL = None

	def setUp(self):
		ensure_test_plan()
		make_test_customer(self.CUSTOMER)
		if self.CUSTOMER_EMAIL:
			frappe.db.set_value("Customer", self.CUSTOMER, "email_id", self.CUSTOMER_EMAIL)
		self._tracked = []
		frappe.db.commit()

	def track(self, doctype, name):
		self._tracked.append((doctype, name))
		return name

	def make_contract(self, submit=False, slug=None, **overrides):
		"""A cloud Contract for the suite's Customer, provisioning patched, tracked."""
		from unittest.mock import patch

		fields = {
			"doctype": "Contract", "party_type": "Customer", "party_name": self.CUSTOMER,
			"start_date": add_days(nowdate(), 14), "contract_terms": "Termos de teste.",
			"mz_subscription_plan": TEST_PLAN, "mz_tenant": slug or "funil-teste",
		}
		fields.update(overrides)
		with patch("ai_saas.saas.provisioning.provision_tenant"):
			doc = frappe.get_doc(fields).insert(ignore_permissions=True)
			if submit:
				doc.submit()
		self.track("Contract", doc.name)
		return doc

	def make_prov(self, contract, status="Active", slug=None, **overrides):
		row = {
			"doctype": "MZ Tenant Provisioning", "contract": contract,
			"tenant_slug": slug or frappe.db.get_value("Contract", contract, "mz_tenant") or "funil-teste",
			"status": status,
		}
		row["site_name"] = overrides.pop("site_name", f"{row['tenant_slug']}.erp.mozeconomia.co.mz")
		row.update(overrides)
		doc = frappe.get_doc(row).insert(ignore_permissions=True)
		self.track("MZ Tenant Provisioning", doc.name)
		return doc

	def _delete_contract(self, name):
		if not frappe.db.exists("Contract", name):
			return
		for p in frappe.get_all("MZ Tenant Provisioning", {"contract": name}, pluck="name"):
			frappe.delete_doc("MZ Tenant Provisioning", p, force=True, ignore_permissions=True)
		sub = frappe.db.get_value("Contract", name, "mz_linked_subscription")
		cleanup_contract(name)
		if sub:
			frappe.delete_doc("Subscription", sub, force=True, ignore_missing=True, ignore_permissions=True)

	def tearDown(self):
		for doctype, name in reversed(self._tracked):
			if doctype == "Contract":
				self._delete_contract(name)
				continue
			if doctype in ("Opportunity", "MZ Overdue Review"):
				for t in frappe.get_all("ToDo", {"reference_type": doctype, "reference_name": name}, pluck="name"):
					frappe.delete_doc("ToDo", t, force=True, ignore_permissions=True)
			frappe.delete_doc(doctype, name, force=True, ignore_missing=True, ignore_permissions=True)
		if frappe.db.exists("Customer", self.CUSTOMER):
			frappe.db.set_value("Customer", self.CUSTOMER, {"customer_primary_contact": None, "email_id": None, "mobile_no": None})
			frappe.delete_doc("Customer", self.CUSTOMER, force=True, ignore_permissions=True)
		frappe.db.commit()
