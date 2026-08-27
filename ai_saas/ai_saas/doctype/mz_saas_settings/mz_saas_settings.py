# Copyright (c) 2026, MozEconomia, SA
# The central configuration of the funnel (docs/sales-funnel-implementation.md, F1).
# Read through ai_saas.saas.tenant_lifecycle.get_settings(), which applies the
# fail-safe defaults — never read fields directly with get_single_value.

from frappe.model.document import Document


class MZSaaSSettings(Document):
	pass
