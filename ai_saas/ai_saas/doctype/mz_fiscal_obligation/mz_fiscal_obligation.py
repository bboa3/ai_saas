# Copyright (c) 2026, MozEconomia, SA
# The fiscal-obligation table behind the Alerta Fiscal (docs/content-layers-implementation.md).
# One row per obligation, written once and legally reviewed; saas/fiscal_alerts.py reads it daily.

import frappe
from frappe.model.document import Document
from frappe.utils import cint


class MZFiscalObligation(Document):
	def validate(self):
		self.code = (self.code or "").strip().upper()
		if not 1 <= cint(self.deadline_day) <= 31:
			frappe.throw("O dia-limite tem de estar entre 1 e 31.")
		if self.periodicity == "Annual" and not 1 <= cint(self.deadline_month) <= 12:
			frappe.throw("Uma obrigação anual precisa do mês-limite (1 a 12).")
		if self.periodicity == "Quarterly" and not 1 <= cint(self.deadline_month) <= 3:
			frappe.throw("Uma obrigação trimestral indica o primeiro mês em que ocorre (1 a 3).")
		if self.enabled and not (self.reviewed_by and self.reviewed_on):
			frappe.throw(
				"Só uma obrigação revista (revisor e data) pode ser activada — nada é enviado sem revisão."
			)
		if self.enabled and not (self.alert_subject and self.alert_body):
			frappe.throw("Uma obrigação activa precisa do assunto e do corpo do email.")
