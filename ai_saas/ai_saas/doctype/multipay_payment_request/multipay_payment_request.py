# Copyright (c) 2026, MozEconomia, SA and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class MultipayPaymentRequest(Document):
	def after_insert(self):
		# Safety net: api.py sets transaction_id explicitly before calling SISLOG,
		# but if a doc is ever created outside that flow this guarantees it is set.
		# after_insert fires after db_insert() so self.name is guaranteed available.
		if not self.transaction_id:
			transaction_id = self.name.replace("-", "")
			self.db_set("transaction_id", transaction_id)
			self.transaction_id = transaction_id
