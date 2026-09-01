"""The one place configuration is read (decided 2026-08-31, workstream B).

`get_settings()` used to live in tenant_lifecycle and the named constants in
install.py, which forced every other module into function-level imports to break
the cycle. This module imports nothing but frappe, so anyone may import it at
the top of the file.
"""

import frappe
from frappe.utils import cint

# The names the app promises exist on every site (created by install.py, read everywhere).
TRIAL_CUSTOMER_GROUP = "Cloud - Trial"
CONTRACT_TEMPLATE_TITLE = "MozEconomia Cloud"
WELCOME_EMAIL_TEMPLATE = "MozEconomia Cloud - Entrega da Conta"
DEFAULT_BOOKING_URL = "https://calendly.com/arlindoboa/chamada-de-ativacao-mozeconomia"


def get_settings():
	"""MZ SaaS Settings with fail-safe defaults.

	get_single_value returns None until the Single is saved once — and the
	fail-safe reading of "never configured" must be dry-run ON.
	"""
	raw = frappe._dict(
		(f, frappe.db.get_single_value("MZ SaaS Settings", f))
		for f in (
			"trial_length_days",
			"commercial_customer_group",
			"auto_suspend",
			"auto_archive",
			"overdue_days_to_suspend",
			"grace_days_to_archive",
			"archive_retention_days",
			"prebilling_reminder_days",
			"overdue_followup_days",
			"minimum_users",
			"ops_alert_recipients",
			"usage_report_recipients",
			"default_sales_user",
			"max_concurrent_trials",
			"max_signups_per_day",
		)
	)
	return frappe._dict(
		trial_length_days=cint(raw.trial_length_days) or 14,
		commercial_customer_group=raw.commercial_customer_group,
		scheduler_plans=frappe.get_all(
			"MZ Scheduler Plan", {"parent": "MZ SaaS Settings", "parenttype": "MZ SaaS Settings"}, pluck="subscription_plan"
		),
		# None (never saved) must read as OFF: nothing executes until someone ticks the box.
		auto_suspend=cint(raw.auto_suspend),
		auto_archive=cint(raw.auto_archive),
		overdue_days_to_suspend=cint(raw.overdue_days_to_suspend) or 33,
		grace_days_to_archive=cint(raw.grace_days_to_archive) or 30,
		archive_retention_days=cint(raw.archive_retention_days) or 180,
		prebilling_reminder_days=cint(raw.prebilling_reminder_days) or 4,
		overdue_followup_days=cint(raw.overdue_followup_days) or 7,
		# Self-service floor only: the desk and the qty fallback in _setup_subscription stay at 1.
		minimum_users=cint(raw.minimum_users) or 2,
		ops_alert_recipients=[
			e.strip() for e in (raw.ops_alert_recipients or "").replace("\n", ",").split(",") if e.strip()
		],
		usage_report_recipients=[
			e.strip() for e in (raw.usage_report_recipients or "").replace("\n", ",").split(",") if e.strip()
		],
		default_sales_user=raw.default_sales_user,
		max_concurrent_trials=cint(raw.max_concurrent_trials) or 20,
		max_signups_per_day=cint(raw.max_signups_per_day) or 10,
	)
