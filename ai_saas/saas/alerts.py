"""Internal alerts to the operations team — one sender for every module.

Recipients: MZ SaaS Settings.ops_alert_recipients; while empty, the Administrator's
email, else contacto@mozeconomia.co.mz. Never raises: an alert that cannot be sent
is logged, and the caller's work is not undone by it.
"""

import frappe

from ai_saas.saas.settings import get_settings


def ops_alert_recipients() -> list:

	configured = get_settings().ops_alert_recipients
	if configured:
		return configured
	return [frappe.db.get_value("User", "Administrator", "email") or "contacto@mozeconomia.co.mz"]


def notify_ops(subject: str, html: str, reference_doctype=None, reference_name=None) -> None:
	try:
		frappe.sendmail(
			recipients=ops_alert_recipients(),
			subject=f"[MozEconomia Cloud] {subject}",
			message=html,
			reference_doctype=reference_doctype,
			reference_name=reference_name,
			delayed=False,
		)
	except Exception:
		frappe.log_error(title=f"AI SaaS: ops alert not sent — {subject[:80]}", message=frappe.get_traceback())
