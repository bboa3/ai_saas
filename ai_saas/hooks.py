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

doc_events = {
	"Contract": {
		"on_submit": "ai_saas.saas.contract_lifecycle.on_contract_signed",
		"on_update_after_submit": "ai_saas.saas.contract_lifecycle.on_contract_signed",
		"on_cancel": "ai_saas.saas.contract_lifecycle.on_contract_cancel",
	},
	"Lead Onboarding": {
		"after_insert": "ai_saas.saas.lead_onboarding.on_lead_onboarding_save",
		"on_update": "ai_saas.saas.lead_onboarding.on_lead_onboarding_save",
	},
	"MZ Customer Feedback": {
		"after_insert": "ai_saas.saas.feedback.on_feedback_submit",
	},
}


scheduler_events = {
	"daily": [
		"ai_saas.saas.billing_monitor.flag_overdue_customers",
		"ai_saas.multipay.tasks.sync_pending_payments",
	],
	"hourly": [
		"ai_saas.saas.provisioning.retry_stuck_provisioning",
	],
}

fixtures = [
	{"dt": "Custom Field", "filters": [["dt", "in", ["Contract"]], ["module", "=", "AI SaaS"]]},
	{"dt": "Notification", "filters": [["name", "like", "AI SaaS%"]]},
	{"dt": "Client Script", "filters": [["name", "like", "AI SaaS%"]]},
	{"dt": "Web Form", "filters": [["name", "=", "lead-onboarding-form"]]},
	{"dt": "Web Form", "filters": [["name", "=", "cloud-feedback"]]},
	{"dt": "Segment Intelligence Map"},
	{"dt": "AI N8N Configuration"},
]
