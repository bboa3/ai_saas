# Copyright (c) 2026, MozEconomia, SA
# One row per trial site per day, written by ai_saas.saas.usage_signals (D1).
# The tenant site is only ever read; this is the control-site copy of what was read.

from frappe.model.document import Document


class MZTenantUsageSnapshot(Document):
	pass
