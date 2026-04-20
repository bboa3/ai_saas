import frappe
from frappe.model.document import Document


class MZTenantProvisioning(Document):
	def before_save(self):
		# Prevent contact_password from being cleared on re-save via UI
		prev = self.get_doc_before_save()
		if prev and prev.contact_password and not self.contact_password:
			self.contact_password = prev.contact_password
