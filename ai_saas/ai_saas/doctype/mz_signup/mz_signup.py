# Copyright (c) 2026, MozEconomia, SA
# A2 (docs/sales-funnel-implementation.md): one row per signup in progress.
# No Guest role — every guest access goes through ai_saas.api.signup's token check.
# The G1 nurture Notifications (Lead Nurture - Dia N) read days-since-`modified`
# and `status == "Started"` off this record; there is no Abandoned state.

import frappe
from frappe.model.document import Document


class MZSignup(Document):
	def before_insert(self):
		if not self.resume_token:
			self.resume_token = frappe.generate_hash(length=32)
		if not self.current_step:
			self.current_step = 1

	@property
	def resume_url(self) -> str:
		return frappe.utils.get_url(f"/registo?token={self.resume_token}")
