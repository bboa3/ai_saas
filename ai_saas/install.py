import json
import frappe


def before_migrate():
	"""Ensure custom=1 child DocTypes exist before fixture sync runs.

	bench migrate order: before_migrate → sync_all → sync_fixtures → after_migrate.
	Tipo de Negocio, Modelo de Negocio, and Modelo de Receita are managed exclusively
	via ensure_child_doctypes() (no JSON files in the app). They must exist before
	sync_fixtures() inserts Segment Intelligence Map records that reference them.
	"""
	ensure_child_doctypes()
	frappe.db.commit()


def after_install():
	"""Run setup tasks after app installation"""
	_remove_stale_fields()
	_sync_custom_fields()
	_sync_property_setters()
	backfill_billing_start()
	backfill_contact_mobile()
	backfill_contact_name()
	backfill_customer_primaries()
	ensure_contract_template()
	ensure_email_templates()
	ensure_booking_url()
	ensure_daily_alerts_hour()
	ensure_trial_customer_group()
	ensure_cloud_plan_flags()
	ensure_scheduler_plans()
	retire_legacy_signup()
	_sync_client_scripts()
	_sync_sales_stages()
	_sync_quality_feedback_templates()
	ensure_multipay_custom_fields()
	ensure_multipay_modes_of_payment()
	ensure_child_doctypes()
	frappe.db.commit()


def after_migrate():
	"""Run setup tasks after migration"""
	_remove_stale_fields()
	_sync_custom_fields()
	_sync_property_setters()
	backfill_billing_start()
	backfill_contact_mobile()
	backfill_contact_name()
	backfill_customer_primaries()
	ensure_contract_template()
	ensure_email_templates()
	ensure_booking_url()
	ensure_daily_alerts_hour()
	ensure_trial_customer_group()
	ensure_cloud_plan_flags()
	ensure_scheduler_plans()
	retire_legacy_signup()
	_sync_client_scripts()
	_sync_sales_stages()
	_sync_quality_feedback_templates()
	ensure_multipay_custom_fields()
	ensure_multipay_modes_of_payment()
	ensure_child_doctypes()
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
	],
	# Replaced the same day by the Item check (MZ SaaS Settings.premium_items).
	"Subscription Plan": ["mz_scheduler_enabled"],
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
	"""Custom Fields live in fixtures/custom_field.json (Contract, Lead, Subscription Plan) and
	are applied by sync_fixtures on install and migrate. This hook only covers the one case
	fixtures cannot: a site where the DocType is missing at fixture time is not our concern —
	Contract, Lead and Subscription Plan are core ERPNext."""
	return


def backfill_contact_mobile():
	"""Contracts created before mz_contact_mobile existed take the Customer's mobile,
	so the trial SMS reaches them. Fills empties only; idempotent."""
	if not frappe.db.has_column("Contract", "mz_contact_mobile"):
		return
	frappe.db.sql(
		"""UPDATE `tabContract` c JOIN `tabCustomer` cu ON cu.name = c.party_name
		   SET c.mz_contact_mobile = cu.mobile_no
		   WHERE c.party_type = 'Customer' AND c.mz_tenant IS NOT NULL AND c.mz_tenant != ''
		     AND (c.mz_contact_mobile IS NULL OR c.mz_contact_mobile = '')
		     AND cu.mobile_no IS NOT NULL AND cu.mobile_no != ''"""
	)


def backfill_contact_name():
	"""Contracts created before mz_contact_name existed take the Customer's primary
	contact's first name, so the relationship emails greet a person. Empties only."""
	if not frappe.db.has_column("Contract", "mz_contact_name"):
		return
	frappe.db.sql(
		"""UPDATE `tabContract` c
		   JOIN `tabCustomer` cu ON cu.name = c.party_name
		   JOIN `tabContact` ct ON ct.name = cu.customer_primary_contact
		   SET c.mz_contact_name = TRIM(CONCAT(IFNULL(ct.first_name, ''), ' ', IFNULL(ct.last_name, '')))
		   WHERE c.party_type = 'Customer' AND c.mz_tenant IS NOT NULL AND c.mz_tenant != ''
		     AND (c.mz_contact_name IS NULL OR c.mz_contact_name = '')
		     AND ct.first_name IS NOT NULL AND ct.first_name != ''"""
	)


DAILY_ALERTS_JOB = "frappe.email.doctype.notification.notification.trigger_daily_alerts"
DAILY_ALERTS_CRON = "0 8 * * *"


def ensure_daily_alerts_hour():
	"""Scheduled customer emails (Days Before/After) go out at 08:00 site time, not 00:00,
	so the time-of-day greeting reads 'Bom dia'. frappe's sync_jobs resets the job to
	'Daily' on every migrate, hence re-pinned here (after_migrate runs after sync_jobs)."""
	name = frappe.db.get_value("Scheduled Job Type", {"method": DAILY_ALERTS_JOB}, "name")
	if not name:
		return
	job = frappe.get_doc("Scheduled Job Type", name)
	if job.frequency != "Cron" or job.cron_format != DAILY_ALERTS_CRON:
		job.frequency = "Cron"
		job.cron_format = DAILY_ALERTS_CRON
		job.save(ignore_permissions=True)


def backfill_billing_start():
	"""E3: contracts signed before the field existed get mz_billing_start from
	their Subscription's start_date, so the re-anchored Pós-Contrato emails keep
	firing for them. Fills empties only; idempotent."""
	if not frappe.db.has_column("Contract", "mz_billing_start"):
		return
	rows = frappe.db.sql(
		"""
		SELECT c.name, s.start_date
		FROM `tabContract` c JOIN `tabSubscription` s ON s.name = c.mz_linked_subscription
		WHERE c.docstatus = 1 AND c.is_signed = 1
			AND (c.mz_billing_start IS NULL OR c.mz_billing_start = '')
			AND s.start_date IS NOT NULL
		""",
		as_dict=True,
	)
	for r in rows:
		frappe.db.set_value("Contract", r.name, "mz_billing_start", r.start_date, update_modified=False)


def backfill_customer_primaries():
	"""Cloud customers created before the funnel set both pointers get them now:
	customer_primary_contact and customer_primary_address, resolved through Dynamic
	Link. ERPNext addresses a customer through those two fields — an invoice email
	dead-ends without the first, a printed invoice has no address without the second.
	Fills empties only; idempotent."""
	from ai_saas.saas.party import ensure_customer_primaries

	if not frappe.db.has_column("Contract", "mz_tenant"):
		return
	names = frappe.db.sql_list(
		"""
		SELECT DISTINCT c.name
		FROM `tabCustomer` c JOIN `tabContract` k ON k.party_name = c.name
		WHERE k.docstatus = 1 AND IFNULL(k.mz_tenant, '') <> ''
			AND (c.customer_primary_contact IS NULL OR c.customer_primary_address IS NULL)
		"""
	)
	for name in names:
		try:
			ensure_customer_primaries(name)
		except Exception:
			frappe.log_error(title=f"AI SaaS: primaries not set for {name}", message=frappe.get_traceback())


WELCOME_EMAIL_TEMPLATE = "MozEconomia Cloud - Entrega da Conta"

# C2: the delivery email. Rendered by provisioning._send_welcome_email with the
# context documented there. Create-if-missing — the copy belongs to the business.
_WELCOME_EMAIL_HTML = """
<div style="font-family:Arial,Helvetica,sans-serif;max-width:600px;margin:0 auto;color:#020202">
  <p>{{ greeting }}</p>
  <p>A conta da <strong>{{ customer_name }}</strong> está pronta em
     <a href="{{ site_url }}" style="color:#008000;font-weight:bold">{{ site_name }}</a>.</p>
  <p><a href="{{ reset_link }}" style="display:inline-block;padding:12px 22px;background:#020202;color:#fff;border-radius:8px;text-decoration:none;font-weight:bold">Definir a minha palavra-passe e entrar</a><br>
     <span style="font-size:12px;color:#5a6270">Utilizador: {{ contact_email }} · esta ligação é de utilização única.</span></p>
  {% if is_signed %}
  <p>O seu plano <strong>{{ plan }}</strong> está activo. As facturas chegam a este email no início de cada período, com <strong>7 dias</strong> de prazo de pagamento.</p>
  {% else %}
  <p>Tem até <strong>{{ trial_end }}</strong> para experimentar tudo — facturas certificadas, clientes, stock, salários — sem qualquer pagamento. Comece pela primeira factura: leva 5 minutos e mostra logo se serve ao seu negócio.</p>
  <p><strong>Activar não custa nada</strong> — a facturação começa no dia em que activar. Quando decidir: <a href="{{ activation_url }}" style="color:#008000;font-weight:bold">activar a minha conta</a>. Plano escolhido: <strong>{{ plan }}</strong> — pode alterá-lo na activação.</p>
  <p>20 minutos connosco: emitimos a primeira factura consigo, sem custo — <a href="{{ booking_url }}" style="color:#008000;font-weight:bold">marcar 20 minutos</a>.</p>
  {% endif %}
  <p style="font-size:13px;color:#5a6270">Responda a este email ou fale connosco: <a href="mailto:cloud@mozeconomia.co.mz" style="color:#008000;font-weight:bold">cloud@mozeconomia.co.mz</a> · WhatsApp +258 87 4444 645</p>
  {{ signature }}
</div>
""".strip()


_WELCOME_EMAIL_SUBJECT = "{% if is_signed %}Bem-vindo à MozEconomia Cloud — conta activa{% else %}A sua conta MozEconomia Cloud está pronta{% endif %}"

_STYLE = "font-family:Arial,Helvetica,sans-serif;max-width:600px;margin:0 auto;color:#020202"
_BTN = "display:inline-block;padding:12px 22px;background:#020202;color:#fff;border-radius:8px;text-decoration:none;font-weight:bold"
_LINK = "color:#008000;font-weight:bold"  # secondary, inline branded link — never a second pill

# The communication language's fixed lines — written once, reused verbatim in the
# Email Templates below and mirrored in fixtures/notification.json.
HELP_EMAIL = "cloud@mozeconomia.co.mz"
HELP_WHATSAPP = "+258 87 4444 645"
SUPPORT_LINE = (
	f'<p style="font-size:13px;color:#5a6270">Responda a este email ou fale connosco: '
	f'<a href="mailto:{HELP_EMAIL}" style="{_LINK}">{HELP_EMAIL}</a> · WhatsApp {HELP_WHATSAPP}</p>'
)
TRIAL_PROMISE = "<p><strong>Activar não custa nada</strong> — a facturação começa no dia em que activar.</p>"
CALL_OFFER = (
	'<p>20 minutos connosco: emitimos a primeira factura consigo, sem custo — '
	f'<a href="{{{{ booking_url }}}}" style="{_LINK}">marcar 20 minutos</a>.</p>'
)
_FOOTER = SUPPORT_LINE + "{{ signature }}</div>"

# F2/F3: the three lifecycle emails (saas/lifecycle_mail.py builds the context).
LIFECYCLE_EMAIL_TEMPLATES = {
	"MozEconomia Cloud - Conta Suspensa": {
		"subject": "A conta MozEconomia Cloud da {{ customer_name }} foi suspensa",
		"html": f'<div style="{_STYLE}">'
		"<p>{{ greeting }}</p>"
		"<p>O acesso à conta da <strong>{{ customer_name }}</strong> em {{ site_name }} foi suspenso hoje"
		"{% if cause == 'trial' %} porque o período experimental terminou em <strong>{{ trial_end }}</strong> sem activação."
		"{% elif cause == 'overdue' %} por falta de pagamento{% if invoice %} da factura <strong>{{ invoice }}</strong>"
		"{% if outstanding %} ({{ outstanding }}{% if due_date %}, vencida em {{ due_date }}{% endif %}){% endif %}{% endif %}."
		"{% else %}.{% endif %}</p>"
		"<p><strong>Os seus dados estão intactos.</strong> Facturas, clientes, artigos, stock — tudo fica exactamente como o deixou. Nada foi apagado.</p>"
		"{% if cause == 'trial' %}"
		"<p>Para voltar a trabalhar basta activar a conta: o acesso é reposto de imediato e a facturação só começa no dia em que activar.</p>"
		f'<p><a href="{{{{ activation_url }}}}" style="{_BTN}">Activar e continuar de onde parei</a></p>'
		"{% elif cause == 'overdue' %}"
		"<p>Para repor o acesso, regularize a factura em atraso — o acesso volta no próprio dia do pagamento. "
		"Se já pagou, responda a este email com o comprovativo e tratamos de imediato.</p>"
		"{% else %}"
		"<p>Para repor o acesso, responda a este email ou fale connosco pelo WhatsApp.</p>"
		"{% endif %}"
		"<p style=\"font-size:13px\"><strong>Importante:</strong> se a conta permanecer suspensa durante {{ grace_days }} dias será arquivada. "
		"Guardamos uma cópia de segurança completa, mas o acesso directo deixa de existir e o restauro passa a ser feito pela nossa equipa.</p>"
		+ _FOOTER,
	},
	"MozEconomia Cloud - Conta Arquivada": {
		"subject": "A conta MozEconomia Cloud da {{ customer_name }} foi arquivada",
		"html": f'<div style="{_STYLE}">'
		"<p>{{ greeting }}</p>"
		"<p>A conta da <strong>{{ customer_name }}</strong> em {{ site_name }} esteve suspensa"
		"{% if suspended_on %} desde {{ suspended_on }}{% endif %} e foi arquivada hoje.</p>"
		"<p>Antes de a desligar fizemos uma <strong>cópia de segurança completa</strong> de todos os dados — facturas, clientes, artigos, documentos anexados. Nada se perdeu.</p>"
		"<p>Guardamos essa cópia durante <strong>12 meses</strong>. Dentro desse prazo, responda a este email ou fale connosco pelo WhatsApp: "
		"a nossa equipa restaura a conta a partir da cópia e a {{ customer_name }} continua exactamente de onde parou.</p>"
		+ _FOOTER,
	},
	"MozEconomia Cloud - Conta Activada": {
		"subject": "A conta da {{ customer_name }} está activa — bem-vindo à MozEconomia Cloud",
		"html": f'<div style="{_STYLE}">'
		"<p>{{ greeting }}</p>"
		"<p>A conta da <strong>{{ customer_name }}</strong> em {{ site_name }} é agora definitiva. "
		"Tudo o que registou continua exactamente onde estava — nada foi migrado, nada se perdeu.</p>"
		f'<p><a href="{{{{ site_url }}}}" style="{_BTN}">Entrar na minha conta</a></p>'
		"<table cellpadding=\"4\" cellspacing=\"0\" style=\"border-collapse:collapse;margin:8px 0\">"
		"<tr><td><strong>Plano</strong></td><td>{{ plan }}</td></tr>"
		"<tr><td><strong>Início da facturação</strong></td><td>{{ billing_start or 'hoje' }}</td></tr>"
		"</table>"
		"<p>A primeira factura chega a este email em {{ billing_start or 'breve' }}, com 7 dias de prazo. "
		"Pagamento por transferência bancária (ABSA, NIB 000200151510200470737) ou E-Mola (+258 87 4444 645), sempre com o número da factura como referência.</p>"
		"<p style=\"font-size:13px\">Precisa de mudar de plano ou de corrigir os dados de facturação? Responda a este email e tratamos no próprio dia.</p>"
		+ _FOOTER,
	},
	"MozEconomia Cloud - Conta Reactivada": {
		"subject": "A conta MozEconomia Cloud da {{ customer_name }} está de volta",
		"html": f'<div style="{_STYLE}">'
		"<p>{{ greeting }}</p>"
		"<p>A conta da <strong>{{ customer_name }}</strong> em {{ site_name }} foi reactivada e já está acessível. "
		"Tudo o que registou está lá, tal como o deixou.</p>"
		f'<p><a href="{{{{ site_url }}}}" style="{_BTN}">Entrar na minha conta</a></p>'
		"{% if is_trial %}"
		"<p>O período experimental foi prolongado até <strong>{{ new_trial_end or trial_end }}</strong>. "
		"Active a conta antes dessa data e não volta a haver interrupção: "
		'<a href="{{ activation_url }}">activar agora</a>.</p>'
		"{% else %}"
		"<p>O seu plano <strong>{{ plan }}</strong> está activo e as facturas continuam a chegar a este email.</p>"
		"{% endif %}"
		+ _FOOTER,
	},
}


def ensure_email_templates():
	"""C2 + F2: the customer emails sent from code live in Email Templates, not in
	Python strings. Create-if-missing — after that the copy belongs to the business."""
	if not frappe.db.exists("DocType", "Email Template"):
		return
	wanted = {
		WELCOME_EMAIL_TEMPLATE: {"subject": _WELCOME_EMAIL_SUBJECT, "html": _WELCOME_EMAIL_HTML},
		**LIFECYCLE_EMAIL_TEMPLATES,
	}
	for name, t in wanted.items():
		if frappe.db.exists("Email Template", name):
			continue
		frappe.get_doc({
			"doctype": "Email Template",
			"name": name,
			"subject": t["subject"],
			"use_html": 1,
			"response_html": t["html"],
		}).insert(ignore_permissions=True)


def push_email_templates():
	"""Overwrite the site's Email Templates with the copy in this file. Not run on migrate
	(the copy belongs to the business after creation) — `bench execute` it when a copy
	revision is shipped: bench --site X execute ai_saas.install.push_email_templates"""
	wanted = {
		WELCOME_EMAIL_TEMPLATE: {"subject": _WELCOME_EMAIL_SUBJECT, "html": _WELCOME_EMAIL_HTML},
		**LIFECYCLE_EMAIL_TEMPLATES,
	}
	for name, t in wanted.items():
		if frappe.db.exists("Email Template", name):
			frappe.db.set_value("Email Template", name, {"subject": t["subject"], "response_html": t["html"], "use_html": 1})
	frappe.db.commit()


DEFAULT_BOOKING_URL = "https://calendly.com/arlindoboa/chamada-de-ativacao-mozeconomia"


def ensure_booking_url():
	"""Calls are booked on Calendly (decision 2026-08-27: ERPNext's /book_appointment is
	not used). The link lives in MZ SaaS Settings so every email reads one place."""
	if not frappe.db.get_single_value("MZ SaaS Settings", "booking_url"):
		frappe.db.set_single_value("MZ SaaS Settings", "booking_url", DEFAULT_BOOKING_URL)


TRIAL_CUSTOMER_GROUP = "Cloud - Trial"


def ensure_trial_customer_group():
	"""A4: trial customers live in their own group so sales reporting can filter them out."""
	if frappe.db.exists("Customer Group", TRIAL_CUSTOMER_GROUP):
		return
	# The root group is translated on pt-MZ sites — resolve it, never assume its name.
	root = frappe.db.get_value(
		"Customer Group", {"is_group": 1, "parent_customer_group": ("in", ["", None])}, "name"
	) or frappe.db.get_value("Customer Group", {"is_group": 1}, "name", order_by="lft asc")
	if not root:
		# App installed before the setup wizard: no tree yet. A parentless group here
		# would become a second root and break the wizard ("Multiple root nodes").
		# Re-run on every migrate, and lazily by the signup API, so it lands later.
		return
	frappe.get_doc({
		"doctype": "Customer Group",
		"customer_group_name": TRIAL_CUSTOMER_GROUP,
		"parent_customer_group": root,
		"is_group": 0,
	}).insert(ignore_permissions=True)


# A5: the second form is retired. Web Form, its DocType and its team notification —
# removed from the site here because sync_fixtures never deletes anything, and
# with for_reload=True delete_doc skips the queue this migrate must not depend on.
def retire_legacy_signup():
	if frappe.db.exists("Web Form", "lead-onboarding-form"):
		frappe.delete_doc("Web Form", "lead-onboarding-form", force=True, ignore_permissions=True, for_reload=True)
	if frappe.db.exists("Notification", "AI SaaS - Lead Form Submetido"):
		frappe.db.delete("Notification Recipient", {"parent": "AI SaaS - Lead Form Submetido"})
		frappe.db.delete("Notification", {"name": "AI SaaS - Lead Form Submetido"})
	if frappe.db.exists("DocType", "Lead Onboarding"):
		frappe.delete_doc("DocType", "Lead Onboarding", force=True, ignore_permissions=True, for_reload=True)
		frappe.db.sql_ddl("DROP TABLE IF EXISTS `tabLead Onboarding`")
	frappe.clear_cache()


def _sync_property_setters():
	"""Property Setters live in fixtures/property_setter.json (Contract.start_date reqd)."""
	return


_CONTRACT_TEMPLATE_TITLE = "MozEconomia Cloud"

# Rendered by erpnext.crm.doctype.contract_template.contract_template.get_contract_template
# against the contract document, so {{ ... }} placeholders are contract fields. This text is
# what the customer accepts at activation — created once, then owned by the business: the
# seeder never overwrites an existing template (B4).
_CONTRACT_TEMPLATE_TERMS = """
<h4>Termos de Serviço — MozEconomia Cloud</h4>
<p>Contrato de prestação de serviços entre a MozEconomia, SA e <strong>{{ party_name }}</strong>.</p>
<ol>
<li><strong>Objecto.</strong> Disponibilização da plataforma MozEconomia Cloud, no plano
<strong>{{ mz_subscription_plan }}</strong>, acessível em {{ mz_tenant_url }}.</li>
<li><strong>Período experimental.</strong> Até {{ frappe.utils.formatdate(start_date) }} a utilização
é gratuita e sem compromisso. A facturação inicia apenas após a assinatura deste contrato,
nunca antes dessa data.</li>
<li><strong>Facturação.</strong> As facturas são emitidas no início de cada período de subscrição,
com prazo de pagamento de 7 dias.</li>
<li><strong>Suspensão.</strong> A falta de pagamento prolongada pode levar à suspensão do acesso.
Os dados permanecem intactos durante a suspensão e a reactivação restaura o serviço integralmente.</li>
<li><strong>Dados.</strong> Os dados pertencem ao cliente. Em caso de encerramento da conta é
efectuada uma cópia de segurança completa antes de qualquer remoção.</li>
<li><strong>Suporte.</strong> cloud@mozeconomia.co.mz · +258 87 4444 645.</li>
</ol>
""".strip()


def ensure_contract_template():
	"""Create the Contract Template programmatic contract creation renders (B4).

	contract_terms is reqd on Contract and only the desk JS fills it from a template —
	an API-created contract must render this template server-side via
	erpnext.crm.doctype.contract_template.contract_template.get_contract_template.
	Create-if-missing only: the terms text belongs to the business after first creation.
	"""
	if not frappe.db.exists("DocType", "Contract Template"):
		return
	if frappe.db.exists("Contract Template", _CONTRACT_TEMPLATE_TITLE):
		return
	frappe.get_doc({
		"doctype": "Contract Template",
		"title": _CONTRACT_TEMPLATE_TITLE,
		"contract_terms": _CONTRACT_TEMPLATE_TERMS,
	}).insert(ignore_permissions=True)


# Client script: renders mz_tenant as a composite input — the user types only
# the slug and the suffix ".erp.mozeconomia.co.mz" is shown attached to the
# right of the input box, making the full domain immediately visible.
# mz_tenant_url (read-only) is kept as the computed full URL for email templates.
_CONTRACT_CLIENT_SCRIPT = """\
frappe.ui.form.on('Contract', {
\trefresh: function(frm) {
\t\t_attach_tenant_suffix(frm);
\t\t_update_tenant_url(frm);
\t\t_populate_default_apps(frm);
\t},
\tmz_tenant: function(frm) {
\t\t_update_tenant_url(frm);
\t},
\tmz_segment: function(frm) {
\t\t_populate_default_apps(frm, true);
\t},
\tmz_subscription_plan: function(frm) {
\t\t_populate_default_apps(frm, true);
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

function _populate_default_apps(frm, force) {
\t// The apps grid follows the segment and the plan: base apps + what Segment Intelligence Map
\t// declares, minus the apps the plan tier does not include (hrms needs Profissional/Premium).
\t// Filled when the document is new / the grid is empty, and again whenever the segment
\t// changes. Sales can still add, remove or reorder rows before submitting.
\tif (!force && !(frm.doc.__islocal || !(frm.doc.mz_apps_to_install || []).length)) return;
\tfrappe.call({
\t\tmethod: 'ai_saas.saas.provisioning.get_apps_for_segment',
\t\targs: { segment: frm.doc.mz_segment || null, plan: frm.doc.mz_subscription_plan || null },
\t\tcallback: function(r) {
\t\t\tvar rows = (r.message || []).map(function(app) { return { app_name: app }; });
\t\t\tfrm.set_value('mz_apps_to_install', rows);
\t\t}
\t});
}
"""


def ensure_cloud_plan_flags():
	"""Seed Subscription Plan.mz_cloud_plan once for the plans self-service offers.
	Only when nothing is flagged yet — afterwards the flag is the team's to manage."""
	if not frappe.db.has_column("Subscription Plan", "mz_cloud_plan"):
		return
	if frappe.db.count("Subscription Plan", {"mz_cloud_plan": 1}):
		return
	for name in frappe.get_all("Subscription Plan", {"name": ("like", "%MozEconomia Cloud%")}, pluck="name"):
		frappe.db.set_value("Subscription Plan", name, "mz_cloud_plan", 1, update_modified=False)


def ensure_scheduler_plans():
	"""C4: seed MZ SaaS Settings.scheduler_plans once with the plans named Premium.
	Afterwards the table is the team's to manage."""
	if not frappe.db.exists("DocType", "MZ Scheduler Plan"):
		return
	settings = frappe.get_single("MZ SaaS Settings")
	if settings.get("scheduler_plans"):
		return
	plans = frappe.get_all("Subscription Plan", {"name": ("like", "%Premium%")}, pluck="name")
	if not plans:
		return
	settings.set("scheduler_plans", [{"subscription_plan": p} for p in plans])
	settings.flags.ignore_permissions = True
	settings.save()


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
# D2 — set by usage_signals from the daily probe of the trial site.
"Cloud - Trial Engaged",
"Cloud - Trial At Risk",
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


def ensure_child_doctypes():
	"""Ensure all AI SaaS child DocTypes exist as custom=1 so the orphan check never deletes them.

	bench migrate's orphan check calls get_controller() for every non-custom DocType and deletes
	any that raise ImportError. These child tables consistently trigger that in the migrate context
	even though their files are present. Setting custom=1 exempts them from the check entirely.
	The JSON files are kept for schema documentation; this function is the authoritative creator.
	"""
	_ensure_child_doctype(
		name="Tipo de Negocio",
		autoname="autoincrement",
		title_field="tipo",
		fields=[{"fieldname": "tipo", "fieldtype": "Data", "label": "Tipo", "reqd": 1, "in_list_view": 1, "unique": 1}],
	)
	_ensure_child_doctype(
		name="Modelo de Negocio",
		autoname="autoincrement",
		title_field="modelo",
		fields=[{"fieldname": "modelo", "fieldtype": "Data", "label": "Modelo", "reqd": 1, "in_list_view": 1}],
	)
	_ensure_child_doctype(
		name="Modelo de Receita",
		autoname="autoincrement",
		title_field="modelo",
		fields=[{"fieldname": "modelo", "fieldtype": "Data", "label": "Modelo", "reqd": 1, "in_list_view": 1}],
	)
	_ensure_child_doctype(
		name="Dore Estruturada",
		autoname="autoincrement",
		title_field="tipo_dore",
		fields=[
			{"fieldname": "tipo_dore", "fieldtype": "Select", "label": "Tipo de Dor", "reqd": 1, "in_list_view": 1,
				"options": "\nOperacional\nFinanceira\nFiscal\nComercial"},
			{"fieldname": "descricao", "fieldtype": "Text", "label": "Descrição", "in_list_view": 1},
			{"fieldname": "severidade_1_5", "fieldtype": "Int", "label": "Severidade (1–5)", "in_list_view": 1,
				"description": "Escala de 1 a 5"},
			{"fieldname": "frequencia", "fieldtype": "Select", "label": "Frequência", "in_list_view": 1,
				"options": "\nCritica\nConstante\nFrequente\nMensal\nTrimestral\nSazonal\nAnual\nOcasional\nRara"},
		],
	)
	_ensure_child_doctype(
		name="Tag Diferenciadora",
		autoname="autoincrement",
		title_field="tipo",
		fields=[
			{"fieldname": "tipo", "fieldtype": "Data", "label": "Tipo", "reqd": 1, "in_list_view": 1},
			{"fieldname": "tags", "fieldtype": "Small Text", "label": "Tags", "in_list_view": 1,
				"description": "Uma tag por linha"},
		],
	)
	_ensure_child_doctype(
		name="Oportunidade Upsell",
		autoname="autoincrement",
		title_field="titulo",
		fields=[
			{"fieldname": "id_upsell", "fieldtype": "Data", "label": "ID Upsell", "reqd": 1, "in_list_view": 1},
			{"fieldname": "categoria", "fieldtype": "Select", "label": "Categoria", "in_list_view": 1,
				"options": "\nAutomacao\nConsultoria\nModulo_Adicional\nServico_Contabilidade"},
			{"fieldname": "titulo", "fieldtype": "Data", "label": "Título", "in_list_view": 1},
			{"fieldname": "proposta_comercial_resumida", "fieldtype": "Text", "label": "Proposta Comercial Resumida"},
			{"fieldname": "roi_estimado_meses", "fieldtype": "Int", "label": "ROI Estimado (meses)"},
			{"fieldname": "preco_min_mzn", "fieldtype": "Currency", "label": "Preço Mín (MZN)"},
			{"fieldname": "preco_max_mzn", "fieldtype": "Currency", "label": "Preço Máx (MZN)"},
			{"fieldname": "confianca_match_1_5", "fieldtype": "Int", "label": "Confiança de Match (1–5)",
				"description": "Escala de 1 a 5"},
			{"fieldname": "pre_requisitos_sistema", "fieldtype": "Small Text", "label": "Pré-requisitos do Sistema"},
		],
	)
	_ensure_child_doctype(
		name="Gatilho Acao",
		autoname="autoincrement",
		title_field="id_gatilho",
		fields=[
			{"fieldname": "id_gatilho", "fieldtype": "Data", "label": "ID Gatilho", "reqd": 1, "in_list_view": 1},
			{"fieldname": "tipo", "fieldtype": "Select", "label": "Tipo", "in_list_view": 1,
				"options": "\ncomportamental\nfinanceiro\noperacional\ntemporal"},
			{"fieldname": "condicao_maquina", "fieldtype": "Text", "label": "Condição (Máquina)"},
			{"fieldname": "fonte_dados", "fieldtype": "Data", "label": "Fonte de Dados"},
			{"fieldname": "n8n_workflow_id", "fieldtype": "Data", "label": "n8n Workflow ID"},
			{"fieldname": "acao_comercial", "fieldtype": "Select", "label": "Ação Comercial", "in_list_view": 1,
				"options": "\nalerta\ncall\nemail\ntask_crm"},
			{"fieldname": "prioridade_execucao", "fieldtype": "Select", "label": "Prioridade de Execução", "in_list_view": 1,
				"options": "\nImediata\n24h\n7d"},
		],
	)


def _ensure_child_doctype(name, autoname, title_field, fields):
	"""Create or repair a child DocType as custom=1."""
	if frappe.db.exists("DocType", name):
		# Repair stale autoname — e.g. previously created with field:X, now needs autoincrement
		if frappe.db.get_value("DocType", name, "autoname") != autoname:
			frappe.db.set_value("DocType", name, "autoname", autoname)
		return
	try:
		frappe.get_doc({
			"doctype": "DocType",
			"name": name,
			"module": "AI SaaS",
			"custom": 1,
			"istable": 1,
			"editable_grid": 1,
			"autoname": autoname,
			"title_field": title_field,
			"fields": fields,
		}).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(
			title=f"AI SaaS: _ensure_child_doctype '{name}' failed",
			message=frappe.get_traceback(),
		)
