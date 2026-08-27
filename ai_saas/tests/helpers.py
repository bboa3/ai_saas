"""Shared fixtures for the funnel tests: every suite used to skip on a fresh site
because the MozEconomia Cloud plans were absent — which made a fresh site silently
"green" with zero tests run. ensure_test_plan() creates what is missing instead."""

import frappe

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
