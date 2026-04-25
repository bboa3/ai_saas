import frappe


def after_install():
	"""Run setup tasks after app installation"""
	_remove_stale_fields()
	_sync_custom_fields()
	_sync_property_setters()
	_sync_client_scripts()
	_sync_sales_stages()
	_sync_quality_feedback_templates()
	ensure_multipay_custom_fields()
	ensure_multipay_modes_of_payment()
	frappe.db.commit()


def after_migrate():
	"""Run setup tasks after migration"""
	_remove_stale_fields()
	_sync_custom_fields()
	_sync_property_setters()
	_sync_client_scripts()
	_sync_sales_stages()
	_sync_quality_feedback_templates()
	ensure_multipay_custom_fields()
	ensure_multipay_modes_of_payment()
	frappe.db.commit()


_STALE_FIELDS = {
	"Contract": [
		"contact_person",
		"mz_billing_cycle",
		"mz_contact_email",
		"mz_customer_email",
		"mz_internal_notes",
		"mz_item",
		"mz_price_override",
		"mz_sales_responsible",
		"mz_sales_responsible_email",
		"mz_service_lines",
		"mz_service_status",
		"mz_technical_responsible",
		# Old field that stored only the slug but was misleadingly named mz_tenant_url.
		# Replaced by the mz_tenant (slug) + mz_tenant_url (full URL, read-only) pair.
		"mz_tenant_url",
	],
	"Sales Invoice": [
		"mz_contract",
		"mz_customer_email",
		"mz_saas_managed",
		"mz_sales_responsible_email",
	],
}


def _remove_stale_fields():
	"""Delete custom fields from previous iterations that are no longer needed."""
	for dt, fieldnames in _STALE_FIELDS.items():
		for fieldname in fieldnames:
			cf = frappe.db.get_value("Custom Field", {"dt": dt, "fieldname": fieldname}, "name")
			if cf:
				frappe.delete_doc("Custom Field", cf, ignore_missing=True, force=True)


def _sync_custom_fields():
	"""Programmatically ensure AI SaaS custom fields on Contract exist."""
	if not frappe.db.exists("DocType", "Contract"):
		return
	try:
		from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

		create_custom_fields(
			{
				"Contract": [
					{
						"fieldname": "mz_saas_tab",
						"label": "MozEconomia Cloud",
						"fieldtype": "Tab Break",
						"insert_after": "fulfilment_terms",
						"module": "AI SaaS",
					},
					{
						"fieldname": "mz_subscription_plan",
						"label": "Plano de Subscrição",
						"fieldtype": "Link",
						"options": "Subscription Plan",
						"insert_after": "mz_saas_tab",
						"module": "AI SaaS",
					},
					{
						# The user types only the slug part — e.g. "boa-construtora"
						# The client script appends ".erp.mozeconomia.co.mz" visually.
						"fieldname": "mz_tenant",
						"label": "Subdomínio do Cliente",
						"fieldtype": "Data",
						"options": "",
						"insert_after": "mz_subscription_plan",
						"module": "AI SaaS",
					},
					{
						# Computed by the client script: mz_tenant + ".erp.mozeconomia.co.mz"
						# Read-only. Stores only the domain (no https://) — the email template
						# prepends https:// when rendering the link.
						"fieldname": "mz_tenant_url",
						"label": "URL de Acesso",
						"fieldtype": "Data",
						"options": "",
						"read_only": 1,
						"insert_after": "mz_tenant",
						"module": "AI SaaS",
					},
					{
						"fieldname": "contact_email",
						"label": "Email de Contacto",
						"fieldtype": "Data",
						"options": "Email",
						"insert_after": "mz_tenant_url",
						"module": "AI SaaS",
					},
					{
						"fieldname": "mz_linked_subscription",
						"label": "Subscrição ERPNext",
						"fieldtype": "Link",
						"options": "Subscription",
						"read_only": 1,
						"insert_after": "contact_email",
						"module": "AI SaaS",
					},
					{
						# The sales team selects which Frappe apps to install for this tenant.
						# Pre-populated with defaults by the client script on form load.
						"fieldname": "mz_apps_to_install",
						"label": "Aplicações a Instalar",
						"fieldtype": "Table",
						"options": "MZ Tenant App",
						"insert_after": "mz_linked_subscription",
						"module": "AI SaaS",
					},
				],
			},
			ignore_validate=True,
			update=True,
		)
	except Exception:
		frappe.log_error(
			title="AI SaaS: Sync Custom Fields Failed",
			message=frappe.get_traceback(),
		)


def _sync_property_setters():
	"""Apply Property Setters that intentionally change standard ERPNext field behaviour for AI SaaS."""
	if not frappe.db.exists("DocType", "Contract"):
		return
	# Contract.start_date must be mandatory — subscriptions cannot be created without it.
	_upsert_property_setter("Contract", "start_date", "reqd", "1", "Check")


def _upsert_property_setter(doctype, fieldname, property, value, property_type):
	"""Delete-then-recreate a Property Setter so re-migrate never raises a duplicate-key error."""
	ps_name = f"{doctype}-{fieldname}-{property}"
	if frappe.db.exists("Property Setter", ps_name):
		frappe.delete_doc("Property Setter", ps_name, ignore_missing=True, force=True)
	frappe.make_property_setter(
		{"doctype": doctype, "fieldname": fieldname, "property": property, "value": value, "property_type": property_type},
		ignore_validate=True,
	)


# Client script: renders mz_tenant as a composite input — the user types only
# the slug and the suffix ".erp.mozeconomia.co.mz" is shown attached to the
# right of the input box, making the full domain immediately visible.
# mz_tenant_url (read-only) is kept as the computed full URL for email templates.
_DEFAULT_APPS = ["erpnext", "hrms", "erpnext_mz", "pos_next", "payments"]

_CONTRACT_CLIENT_SCRIPT = """\
var _DEFAULT_APPS = ['erpnext', 'hrms', 'erpnext_mz', 'pos_next', 'payments'];

frappe.ui.form.on('Contract', {
\trefresh: function(frm) {
\t\t_attach_tenant_suffix(frm);
\t\t_update_tenant_url(frm);
\t\t_populate_default_apps(frm);
\t},
\tmz_tenant: function(frm) {
\t\t_update_tenant_url(frm);
\t}
});

function _attach_tenant_suffix(frm) {
\tvar field = frm.fields_dict['mz_tenant'];
\tif (!field || !field.$input || field._mz_suffix_attached) return;
\tfield._mz_suffix_attached = true;

\tvar $input = field.$input;
\t// Make the input and suffix look like a single combined widget
\t$input.css({
\t\t'border-top-right-radius': '0',
\t\t'border-bottom-right-radius': '0',
\t\t'border-right': 'none'
\t});
\tvar $suffix = $('<span>', {
\t\ttext: '.erp.mozeconomia.co.mz',
\t\tcss: {
\t\t\tdisplay: 'inline-flex',
\t\t\t'align-items': 'center',
\t\t\tpadding: '0 10px',
\t\t\tbackground: '#f5f7fa',
\t\t\tborder: '1px solid #d1d8dd',
\t\t\t'border-left': 'none',
\t\t\t'border-radius': '0 4px 4px 0',
\t\t\t'font-size': '13px',
\t\t\tcolor: '#6c7680',
\t\t\t'white-space': 'nowrap',
\t\t\t'line-height': '1'
\t\t}
\t});
\t$input.parent().css('display', 'flex');
\t$input.after($suffix);
}

function _update_tenant_url(frm) {
\tvar slug = (frm.doc.mz_tenant || '').trim().toLowerCase();
\tvar url = slug ? slug + '.erp.mozeconomia.co.mz' : '';
\tif (frm.doc.mz_tenant_url !== url) {
\t\tfrm.set_value('mz_tenant_url', url);
\t}
}

function _populate_default_apps(frm) {
\t// Pre-fill the apps table with defaults on new documents or when the list is empty.
\t// The sales team can then add, remove, or reorder apps before signing the contract.
\tif (frm.doc.__islocal || !(frm.doc.mz_apps_to_install || []).length) {
\t\tvar rows = _DEFAULT_APPS.map(function(app) {
\t\t\treturn { app_name: app };
\t\t});
\t\tfrm.set_value('mz_apps_to_install', rows);
\t}
}
"""


def _sync_client_scripts():
	"""Create or update the AI SaaS client script on Contract."""
	script_name = "AI SaaS - Contract"
	if frappe.db.exists("Client Script", script_name):
		frappe.db.set_value("Client Script", script_name, {
			"script": _CONTRACT_CLIENT_SCRIPT,
			"enabled": 1,
		})
	else:
		frappe.get_doc({
			"doctype": "Client Script",
			"name": script_name,
			"dt": "Contract",
			"script": _CONTRACT_CLIENT_SCRIPT,
			"enabled": 1,
		}).insert(ignore_permissions=True)


_CLOUD_SALES_STAGES = [
	"Cloud - Account Created",
	"Cloud - Qualified - Awaiting Setup",
	"Cloud - Form Submitted",
	"Cloud - Form Started",
	"Cloud - Nurturing",
]


def _sync_sales_stages():
	"""Ensure the Cloud sales-stage records exist. Idempotent — safe on every migrate."""
	for stage in _CLOUD_SALES_STAGES:
		if not frappe.db.exists("Sales Stage", stage):
			frappe.get_doc({"doctype": "Sales Stage", "stage_name": stage}).insert(ignore_permissions=True)


_QUALITY_FEEDBACK_TEMPLATES = {
	"MZ Cloud - Primeiros Passos": ["Experiência inicial com o MozEconomia Cloud"],
	"MZ Cloud - Primeiro Mês": ["Avaliação do primeiro mês com o MozEconomia Cloud"],
}


def _sync_quality_feedback_templates():
	"""Ensure Quality Feedback Templates for the post-contract feedback cycle exist."""
	if not frappe.db.exists("DocType", "Quality Feedback Template"):
		return
	for template_name, parameters in _QUALITY_FEEDBACK_TEMPLATES.items():
		if frappe.db.exists("Quality Feedback Template", template_name):
			continue
		doc = frappe.get_doc({
			"doctype": "Quality Feedback Template",
			"template": template_name,
			"parameters": [{"parameter": p} for p in parameters],
		})
		doc.insert(ignore_permissions=True)


def ensure_multipay_custom_fields():
	"""Add Multipay-related custom fields to Payment Request and Sales Invoice.

	Not managed via fixtures to avoid the fixture-sync rollback bug.
	"""
	if not frappe.db.exists("DocType", "Payment Request"):
		return
	try:
		from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

		create_custom_fields(
			{
				"Payment Request": [
					{
						"fieldname": "mpay_section",
						"label": "Multipay (SISLOG)",
						"fieldtype": "Section Break",
						"insert_after": "message",
						"module": "AI SaaS",
					},
					{
						"fieldname": "mpay_method",
						"label": "Canal de Pagamento",
						"fieldtype": "Select",
						"options": "\nE-Mola\nM-Pesa",
						"insert_after": "mpay_section",
						"module": "AI SaaS",
					},
					{
						"fieldname": "mpay_status",
						"label": "Estado Multipay",
						"fieldtype": "Select",
						"options": "\nPending\nPaid\nFailed\nExpired",
						"insert_after": "mpay_method",
						"module": "AI SaaS",
					},
					{
						"fieldname": "mpay_transaction_id",
						"label": "ID de Transação SISLOG",
						"fieldtype": "Data",
						"read_only": 1,
						"unique": 1,
						"insert_after": "mpay_status",
						"module": "AI SaaS",
					},
					{
						"fieldname": "mpay_column_break",
						"fieldtype": "Column Break",
						"insert_after": "mpay_transaction_id",
						"module": "AI SaaS",
					},
					{
						"fieldname": "mpay_entity",
						"label": "Entidade SISLOG",
						"fieldtype": "Data",
						"read_only": 1,
						"insert_after": "mpay_column_break",
						"module": "AI SaaS",
					},
					{
						"fieldname": "mpay_reference",
						"label": "Referência SISLOG",
						"fieldtype": "Data",
						"read_only": 1,
						"insert_after": "mpay_entity",
						"module": "AI SaaS",
					},
					{
						"fieldname": "mpay_payment_entry",
						"label": "Entrada de Pagamento",
						"fieldtype": "Link",
						"options": "Payment Entry",
						"read_only": 1,
						"insert_after": "mpay_reference",
						"module": "AI SaaS",
					},
					{
						"fieldname": "mpay_sislog_raw",
						"label": "Resposta SISLOG (Raw)",
						"fieldtype": "Small Text",
						"read_only": 1,
						"insert_after": "mpay_payment_entry",
						"module": "AI SaaS",
					},
				],
				"Sales Invoice": [
					{
						"fieldname": "mz_latest_multipay_request",
						"label": "Último Pedido Multipay",
						"fieldtype": "Link",
						"options": "Payment Request",
						"read_only": 1,
						"insert_after": "against_income_account",
						"module": "AI SaaS",
					},
				],
			},
			ignore_validate=True,
			update=True,
		)
	except Exception:
		frappe.log_error(
			title="AI SaaS: ensure_multipay_custom_fields failed",
			message=frappe.get_traceback(),
		)


def ensure_multipay_modes_of_payment():
	"""Ensure E-Mola and M-Pesa exist as Modes of Payment."""
	for mop in ("E-Mola", "M-Pesa"):
		if not frappe.db.exists("Mode of Payment", mop):
			try:
				frappe.get_doc({"doctype": "Mode of Payment", "mode_of_payment": mop, "type": "Electronic"}).insert(
					ignore_permissions=True
				)
			except Exception:
				frappe.log_error(
					title=f"AI SaaS: Could not create Mode of Payment '{mop}'",
					message=frappe.get_traceback(),
				)
