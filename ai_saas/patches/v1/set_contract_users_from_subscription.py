"""
Per-user pricing backfill: stamp Contract.mz_users from the linked Subscription's
plan-row qty, under the first-user-included rule (billed qty = mz_users - 1, so
seats = qty + 1).

No Subscription is touched and no bill changes by a single metical: a flat qty-1
customer becomes a 2-seat contract at the same cost (1 paid + the included first
user — exactly the new self-service entry shape), and Sanlo's hand-set qty 5
becomes 6 seats. Where the recorded seats should differ (e.g. Sanlo really has 5
users -> 4 paid), Sales sets mz_users in the desk and the sync adjusts the bill.
Unsigned in-flight contracts (no linked Subscription) are left empty by design —
/activar asks the seats question, defaulting to the minimum, when they convert.
"""

import frappe
from frappe.utils import cint


def execute():
	if not frappe.get_meta("Contract").has_field("mz_users"):
		# Patches run before sync_fixtures on migrate, so on the first run the field
		# does not exist yet. Create it here; the fixture (same name) syncs the full
		# definition right after, idempotently.
		from frappe.custom.doctype.custom_field.custom_field import create_custom_field

		create_custom_field(
			"Contract",
			{
				"fieldname": "mz_users",
				"fieldtype": "Int",
				"label": "Número de Utilizadores",
				"insert_after": "mz_subscription_plan",
				"allow_on_submit": 1,
				"non_negative": 1,
			},
		)
		frappe.clear_cache(doctype="Contract")

	contracts = frappe.get_all(
		"Contract",
		filters={"docstatus": 1, "mz_linked_subscription": ("is", "set")},
		fields=["name", "mz_linked_subscription"],
	)
	for c in contracts:
		qty = cint(
			frappe.db.get_value("Subscription Plan Detail", {"parent": c.mz_linked_subscription}, "qty")
		)
		if not qty:
			# Dangling link (subscription deleted) — leave the contract alone.
			continue
		frappe.db.set_value("Contract", c.name, "mz_users", qty + 1, update_modified=False)
