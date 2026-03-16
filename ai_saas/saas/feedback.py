import frappe


def on_feedback_submit(doc, method=None):
	"""After MZ Customer Feedback insert: populate customer_name and create Quality Feedback record."""
	# Auto-fill customer_name from Contract
	if doc.contract and not doc.customer_name:
		party_name = frappe.db.get_value("Contract", doc.contract, "party_name")
		if party_name:
			frappe.db.set_value("MZ Customer Feedback", doc.name, "customer_name", party_name, update_modified=False)

	# Map feedback_type to Quality Feedback Template name
	template_map = {
		"Primeiros Passos": "MZ Cloud - Primeiros Passos",
		"Primeiro Mês": "MZ Cloud - Primeiro Mês",
	}
	template_name = template_map.get(doc.feedback_type)
	if not template_name:
		return

	# Resolve Customer name for Quality Feedback (document_type = "Customer")
	customer = frappe.db.get_value("Contract", doc.contract, "party_name") if doc.contract else None
	if not customer:
		frappe.log_error(
			title="MZ Feedback: Customer não encontrado",
			message=f"Contrato {doc.contract} não tem party_name.",
		)
		return

	# Build Quality Feedback parameters from the template
	template_doc = frappe.get_doc("Quality Feedback Template", template_name)
	qf = frappe.get_doc({
		"doctype": "Quality Feedback",
		"template": template_name,
		"document_type": "Customer",
		"document_name": customer,
		"parameters": [
			{
				"parameter": p.parameter,
				"rating": str(doc.rating),
				"feedback": doc.feedback_text or "",
			}
			for p in template_doc.parameters
		],
	})
	qf.insert(ignore_permissions=True)
	frappe.db.set_value("MZ Customer Feedback", doc.name, "quality_feedback", qf.name, update_modified=False)
