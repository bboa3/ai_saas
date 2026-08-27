app_name = "ai_saas"
app_title = "AI SaaS"
app_publisher = "MozEconomia, SA"
app_description = "Gestão de marketing, contratos e clientes SaaS para MozEconomia Cloud"
app_email = "contacto@mozeconomia.co.mz"
app_license = "mit"
app_version = "1.0.0"

required_apps = ["erpnext", "erpnext_mz"]

after_install = "ai_saas.install.after_install"
before_migrate = "ai_saas.install.before_migrate"
after_migrate = "ai_saas.install.after_migrate"
before_tests = "ai_saas.tests.helpers.before_tests"

doc_events = {
	"Contract": {
		"on_submit": "ai_saas.saas.contract_lifecycle.on_contract_submitted",
		"on_update_after_submit": "ai_saas.saas.contract_lifecycle.on_contract_signed",
		"on_cancel": "ai_saas.saas.contract_lifecycle.on_contract_cancel",
	},
	"MZ Customer Feedback": {
		"after_insert": "ai_saas.saas.feedback.on_feedback_submit",
	},
	"Payment Entry": {
		"before_submit": "ai_saas.payment_entry.set_contact_email",
	},
	"Sales Invoice": {
		"before_submit": "ai_saas.sales_invoice.set_contact_mobile",
	},
}


scheduler_events = {
	"daily": [
		"ai_saas.saas.billing_monitor.flag_overdue_customers",
		"ai_saas.saas.tenant_lifecycle.process_lifecycle",
		"ai_saas.saas.usage_signals.collect_usage_snapshots",
		"ai_saas.multipay.tasks.sync_pending_payments",
	],
	"hourly": [
		"ai_saas.saas.provisioning.retry_stuck_provisioning",
	],
}

# Helpers available to Email Templates and Notifications (get_activation_url).
jinja = {"methods": "ai_saas.utils.jinja"}

fixtures = [
	{"dt": "Custom Field", "filters": [["dt", "in", ["Contract", "Lead", "Subscription Plan"]], ["module", "=", "AI SaaS"]]},
	{"dt": "Property Setter", "filters": [["name", "in", ["Contract-start_date-reqd"]]]},
	{"dt": "Notification", "filters": [["name", "like", "AI SaaS%"]]},
	{"dt": "Web Form", "filters": [["name", "in", ["cloud-feedback"]]]},
	{"dt": "Segment Intelligence Map"},
	{"dt": "AI N8N Configuration"},
]
