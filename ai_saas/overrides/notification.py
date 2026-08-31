"""A Notification whose rendered body decides whether it sends (workstream B, 2026-08-31).

Frappe's Notification `condition` runs in a frappe.utils-only namespace — it cannot
ask the database anything — so several of our templates gate themselves with a
`{% if ... %}` around the whole body (G2/G3 need a resolvable Contract; the trial
countdown needs the site to actually be Active). Stock Frappe would still send the
empty shell that renders. This override makes an empty rendering mean "do not send",
for email and SMS alike, turning the body-gate into a real gate.
"""

import frappe
from frappe.email.doctype.notification.notification import Notification
from frappe.utils import strip_html


class SilentWhenEmptyNotification(Notification):
	def _renders_empty(self, context) -> bool:
		message = frappe.render_template(self.message or "", context)
		return not strip_html(message).strip()

	def send_an_email(self, doc, context):
		if self._renders_empty(context):
			return
		super().send_an_email(doc, context)

	def send_sms(self, doc, context):
		if self._renders_empty(context):
			return
		super().send_sms(doc, context)
