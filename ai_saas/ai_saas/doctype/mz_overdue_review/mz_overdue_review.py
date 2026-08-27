# Copyright (c) 2026, MozEconomia, SA
# F4 (docs/sales-funnel-implementation.md): the review queue's states execute.
# This is how the team brings a switch-off forward or reverses it by hand.
# Manual actions always execute; the auto_suspend / auto_archive switches in
# MZ SaaS Settings arm only the daily engine.

import frappe
from frappe.model.document import Document


class MZOverdueReview(Document):
	def on_update(self):
		before = self.get_doc_before_save()
		if before and before.review_status == self.review_status:
			return  # not a state transition — notes edit, assignment, etc.
		if self.review_status not in ("Suspend", "Reactivate", "Deactivate"):
			return
		if not self.contract:
			frappe.throw("Esta acção precisa de um contrato ligado ao registo de revisão.")

		from ai_saas.saas.tenant_lifecycle import reactivate, suspend

		# A person acting on a specific record IS the safety check.
		if self.review_status == "Suspend":
			suspend(self.contract, reason=f"Manual via {self.name}", cause="overdue")
		elif self.review_status == "Reactivate":
			# tenant_lifecycle enforces the new-date rule for unsigned trials; force
			# overrides the settled-debt rule — an explicit human decision.
			reactivate(self.contract, new_start_date=self.new_trial_end_date, force=True)
		elif self.review_status == "Deactivate":
			# Suspend now; the daily engine archives after the grace period.
			suspend(self.contract, reason=f"Desactivação manual via {self.name}", cause="overdue")
