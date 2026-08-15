import json
import os
import re
import secrets
import string
import subprocess

import frappe
from frappe.utils.password import get_decrypted_password

_DEFAULT_BENCH_PATH = "/home/frappe/frappe-bench"
_DEFAULT_BENCH_CMD = "/usr/local/bin/bench"


def _get_bench_path() -> str:
	return frappe.conf.get("bench_path") or _DEFAULT_BENCH_PATH


def _get_bench_cmd() -> str:
	return frappe.conf.get("bench_cmd") or _DEFAULT_BENCH_CMD
DOMAIN_SUFFIX = ".erp.mozeconomia.co.mz"
MAX_ATTEMPTS = 3
PROVISION_TIMEOUT = 1200  # 20 min — bench new-site + app installs
WIZARD_TIMEOUT = 300

# Fallback used only when the Contract has no apps configured
DEFAULT_APPS = ["erpnext", "hrms", "erpnext_mz"]

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,38}[a-z0-9]$")
RESERVED_SLUGS = {"www", "mail", "smtp", "ftp", "admin", "api", "erp", "test","teste", "staging", "assets"}


class ProvisioningError(Exception):
	"""A provisioning step failed for this attempt."""


# ---------------------------------------------------------------------------
# Public entry point — called from contract_lifecycle.on_contract_signed
# ---------------------------------------------------------------------------

def provision_tenant(contract_name: str) -> None:
	"""Queue site provisioning for the tenant slug on the given contract.

	Idempotent: silently returns if a provisioning record already exists.
	Raises frappe.ValidationError if the slug is invalid (surfaces to the user).
	"""
	contract = frappe.get_doc("Contract", contract_name)
	slug = (contract.get("mz_tenant") or "").strip().lower()

	if not slug:
		return

	existing = frappe.db.get_value("MZ Tenant Provisioning", {"contract": contract_name}, "name")
	if existing:
		return

	_validate_slug(slug)

	customer_name = (
		frappe.db.get_value("Customer", contract.party_name, "customer_name")
		or contract.party_name
	)
	contact_password = _generate_admin_password()
	contact_email = _resolve_contact_email(contract)

	# Read app list from the Contract; fall back to DEFAULT_APPS if none configured
	apps = [row.app_name for row in (contract.get("mz_apps_to_install") or []) if row.app_name]
	if not apps:
		apps = list(DEFAULT_APPS)

	prov = frappe.get_doc({
		"doctype": "MZ Tenant Provisioning",
		"contract": contract_name,
		"tenant_slug": slug,
		"site_name": slug + DOMAIN_SUFFIX,
		"customer_name": customer_name,
		"contact_email": contact_email,
		"contact_password": contact_password,
		"status": "Queued",
		"attempts": 0,
		"mz_provisioning_apps": [{"app_name": a} for a in apps],
		"log": f"[{frappe.utils.now()}] Provisionamento enfileirado para contrato {contract_name}\n"
			   f"[{frappe.utils.now()}] Email de contacto: {contact_email or 'NÃO ENCONTRADO'}\n"
			   f"[{frappe.utils.now()}] Aplicações: {', '.join(apps)}\n",
	})
	prov.insert(ignore_permissions=True)
	frappe.db.commit()

	frappe.enqueue(
		"ai_saas.saas.provisioning._run_provisioning",
		queue="long",
		timeout=PROVISION_TIMEOUT + 120,
		job_id=f"provision-tenant-{prov.name}",
		deduplicate=True,
		provisioning_name=prov.name,
	)


# ---------------------------------------------------------------------------
# Hourly scheduler — recover stuck jobs after worker crashes
# ---------------------------------------------------------------------------

def retry_stuck_provisioning() -> None:
	"""Re-queue provisioning records stuck in an in-progress state for >30 min."""
	stale_threshold = frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-30)
	stuck = frappe.db.get_all(
		"MZ Tenant Provisioning",
		filters={
			"status": ["in", ["Queued", "Creating Site", "Running Setup Wizard", "Notifying Customer"]],
			"modified": ["<", stale_threshold],
			"attempts": ["<", MAX_ATTEMPTS],
		},
		fields=["name", "attempts"],
	)
	for row in stuck:
		frappe.enqueue(
			"ai_saas.saas.provisioning._run_provisioning",
			queue="long",
			timeout=PROVISION_TIMEOUT + 120,
			job_id=f"provision-tenant-{row.name}-stuck-retry",
			provisioning_name=row.name,
		)


# ---------------------------------------------------------------------------
# Background worker — orchestrates the full provisioning flow
# ---------------------------------------------------------------------------

def _run_provisioning(provisioning_name: str) -> None:
	prov = frappe.get_doc("MZ Tenant Provisioning", provisioning_name)

	if prov.status == "Active":
		return
	if (prov.attempts or 0) >= MAX_ATTEMPTS:
		return

	prov.attempts = (prov.attempts or 0) + 1
	_append_log(prov, f"Tentativa {prov.attempts} iniciada")
	prov.save(ignore_permissions=True)
	frappe.db.commit()

	try:
		_step_create_site(prov)
		_step_apply_system_settings(prov)
		_step_run_setup_wizard(prov)
		_step_seed_company_profile(prov)
		_step_setup_smtp(prov)
		_step_notify_customer(prov)

		prov.status = "Active"
		prov.provisioned_at = frappe.utils.now()
		_append_log(prov, "Provisionamento concluído com sucesso.")
		prov.save(ignore_permissions=True)
		frappe.db.commit()

	except Exception as exc:
		_handle_failure(prov, str(exc))


# ---------------------------------------------------------------------------
# Step functions
# ---------------------------------------------------------------------------

def _step_create_site(prov) -> None:
	prov.status = "Creating Site"
	_append_log(prov, f"A criar site {prov.site_name}")
	prov.save(ignore_permissions=True)
	frappe.db.commit()

	site_dir = os.path.join(_get_bench_path(), "sites", prov.site_name)
	if os.path.exists(site_dir):
		_append_log(prov, "Diretório do site já existe — a saltar bench new-site (tentativa anterior).")
		return

	db_root_password = _get_db_root_password()
	site_admin_password = _get_site_admin_password()

	apps = [row.app_name for row in (prov.get("mz_provisioning_apps") or []) if row.app_name]
	if not apps:
		apps = list(DEFAULT_APPS)

	install_app_args = []
	for app in apps:
		install_app_args += ["--install-app", app]

	_run_cmd(
		[
			_get_bench_cmd(), "new-site", prov.site_name,
			"--db-root-password", db_root_password,
			"--admin-password", site_admin_password,
			*install_app_args,
			"--mariadb-user-host-login-scope", "%",
			"--no-mariadb-socket",
		],
		step="new-site",
		prov=prov,
		timeout=PROVISION_TIMEOUT,
	)
	_run_cmd(
		[_get_bench_cmd(), "--site", prov.site_name, "set-config", "host_name", f"https://{prov.site_name}"],
		step="set-hostname",
		prov=prov,
		timeout=15,
	)
	_append_log(prov, f"Site {prov.site_name} criado, aplicações instaladas e host_name configurado.")


def _step_run_setup_wizard(prov) -> None:
	prov.status = "Running Setup Wizard"
	_append_log(prov, "A executar o assistente de configuração")
	prov.save(ignore_permissions=True)
	frappe.db.commit()

	year = frappe.utils.getdate().year
	contact_email = prov.contact_email or f"admin@{prov.site_name}"
	wizard_kwargs = {
		"country": "Mozambique",
		"language": "pt-MZ",
		"timezone": "Africa/Maputo",
		"currency": "MZN",
		"email": contact_email,
		"full_name": prov.customer_name,
		"password": _get_contact_password(prov),
		"company_name": prov.customer_name,
		"company_abbr": _make_company_abbr(prov.customer_name),
		"fy_start_date": f"{year}-01-01",
		"fy_end_date": f"{year}-12-31",
	}

	# setup_complete(args) takes a single positional parameter.
	# bench execute --kwargs expands kwargs as **kwargs, so we must nest the
	# wizard data under the key "args" so bench calls setup_complete(args={...}).
	_run_cmd(
		[
			_get_bench_cmd(), "--site", prov.site_name,
			"execute",
			"frappe.desk.page.setup_wizard.setup_wizard.setup_complete",
			"--kwargs", json.dumps({"args": wizard_kwargs}),
		],
		step="setup-wizard",
		prov=prov,
		timeout=WIZARD_TIMEOUT,
	)
	_append_log(prov, "Assistente de configuração concluído.")


def _step_apply_system_settings(prov) -> None:
	"""Ensure pt-MZ language record exists and apply system-wide Mozambique defaults
	BEFORE setup_complete runs.  setup_complete assigns wizard args.language to the
	newly created User record; if the Language 'pt-MZ' does not exist at that moment
	Frappe silently falls back to 'en'.  Running ensure_language_pt_mz first
	guarantees the Language record is present, and apply_system_settings primes
	System Settings so the wizard inherits the correct defaults.
	"""
	_append_log(prov, "A garantir registo de idioma pt-MZ no novo site")
	_run_cmd(
		[
			_get_bench_cmd(), "--site", prov.site_name,
			"execute",
			"erpnext_mz.setup.language.ensure_language_pt_mz",
		],
		step="ensure-language",
		prov=prov,
		timeout=30,
	)

	_append_log(prov, "A aplicar definições de sistema (idioma pt-MZ, MZN, fuso horário)")
	_run_cmd(
		[
			_get_bench_cmd(), "--site", prov.site_name,
			"execute",
			"erpnext_mz.setup.language.apply_system_settings",
			"--kwargs", '{"override": 1}',
		],
		step="apply-system-settings",
		prov=prov,
		timeout=60,
	)
	_append_log(prov, "Definições de sistema aplicadas.")


def _step_seed_company_profile(prov) -> None:
	"""Push company data collected from the MozEconomia Customer record to the
	new site's MZ Company Setup so the onboarding wizard opens pre-filled."""
	_append_log(prov, "A pré-preencher perfil da empresa no novo site")

	data = _collect_customer_profile(prov)
	if not data:
		_append_log(prov, "AVISO: Sem dados de cliente para pré-preencher — passo ignorado.")
		return

	_run_cmd(
		[
			_get_bench_cmd(), "--site", prov.site_name,
			"execute",
			"erpnext_mz.setup.onboarding.seed_company_profile",
			"--kwargs", json.dumps({"data": data}),
		],
		step="seed-company-profile",
		prov=prov,
		timeout=60,
	)
	_append_log(prov, f"Perfil pré-preenchido: {', '.join(data.keys())}")


def _collect_customer_profile(prov) -> dict:
	"""Fetch NUIT, address, and contact fields from the Customer linked to the contract.

	Returns a dict keyed by MZ Company Setup field names. Only non-empty values are
	included so we never overwrite real data with blank strings.

	Priority order for phone/email:
	  1. prov.contact_email (explicitly set on the contract)
	  2. Primary Contact linked to the Customer
	  3. Customer.email_id / Customer.mobile_no direct fields
	"""
	try:
		contract = frappe.get_doc("Contract", prov.contract)
		customer_name = contract.party_name
		if not customer_name:
			return {}

		data = {}

		# ── Direct Customer fields ──────────────────────────────────────────────
		customer = frappe.db.get_value(
			"Customer",
			customer_name,
			["tax_id", "mobile_no", "email_id", "website"],
			as_dict=True,
		)
		if customer:
			if customer.tax_id:
				data["tax_id"] = customer.tax_id
			if customer.mobile_no:
				data["phone"] = customer.mobile_no
			if customer.email_id:
				data["email"] = customer.email_id
			if customer.website:
				data["website"] = customer.website

		# ── Primary Contact linked to the Customer ──────────────────────────────
		# In ERPNext, phone and email are usually stored on a Contact record, not
		# directly on the Customer.  The primary contact has is_primary_contact=1.
		contact_name = frappe.db.get_value(
			"Dynamic Link",
			{
				"link_doctype": "Customer",
				"link_name": customer_name,
				"parenttype": "Contact",
			},
			"parent",
		)
		if contact_name:
			contact = frappe.db.get_value(
				"Contact",
				contact_name,
				["email_id", "phone", "mobile_no"],
				as_dict=True,
			)
			if contact:
				if contact.email_id and not data.get("email"):
					data["email"] = contact.email_id
				phone = contact.mobile_no or contact.phone
				if phone and not data.get("phone"):
					data["phone"] = phone

		# ── Contract contact_email always wins ──────────────────────────────────
		if prov.contact_email:
			data["email"] = prov.contact_email

		# ── Primary Address linked to the Customer ──────────────────────────────
		# Try billing address first; fall back to any linked address.
		address_name = (
			frappe.db.get_value(
				"Dynamic Link",
				{
					"link_doctype": "Customer",
					"link_name": customer_name,
					"parenttype": "Address",
					"parent": ("in", frappe.db.get_all(
						"Address",
						filters={"address_type": "Billing"},
						pluck="name",
					) or ["__none__"]),
				},
				"parent",
			)
			or frappe.db.get_value(
				"Dynamic Link",
				{"link_doctype": "Customer", "link_name": customer_name, "parenttype": "Address"},
				"parent",
			)
		)
		if address_name:
			addr = frappe.db.get_value(
				"Address",
				address_name,
				["address_line1", "address_line2", "city", "state", "phone"],
				as_dict=True,
			)
			if addr:
				if addr.address_line1:
					data["address_line1"] = addr.address_line1
				if addr.address_line2:
					data["neighborhood_or_district"] = addr.address_line2
				if addr.city:
					data["city"] = addr.city
				if addr.state:
					data["province"] = addr.state
				if addr.phone and not data.get("phone"):
					data["phone"] = addr.phone

		return data
	except Exception:
		frappe.log_error(frappe.get_traceback(), "AI SaaS: _collect_customer_profile failed")
		return {}


def _step_setup_smtp(prov) -> None:
	"""Configure outbound email on the new site so the customer can receive password-reset
	emails after the initial reset link expires. Non-fatal: logs a warning on failure so
	that a misconfigured SMTP server never blocks the provisioning from completing."""
	_append_log(prov, "A configurar email no novo site")
	try:
		_run_cmd(
			[
				_get_bench_cmd(), "--site", prov.site_name,
				"execute",
				"erpnext_mz.setup.onboarding.ensure_smtp_infrastructure_manually",
			],
			step="setup-smtp",
			prov=prov,
			timeout=60,
		)
		_append_log(prov, "Infraestrutura de email configurada no novo site.")
	except ProvisioningError as exc:
		_append_log(prov, f"AVISO: Configuração de email falhou (não bloqueante): {exc}")
		frappe.log_error(
			title=f"AI SaaS: SMTP setup skipped for {prov.site_name}",
			message=str(exc),
		)


def _step_notify_customer(prov) -> None:
	prov.status = "Notifying Customer"
	_append_log(prov, "A gerar link de acesso para o utilizador")
	prov.save(ignore_permissions=True)
	frappe.db.commit()

	if not prov.contact_email:
		_append_log(prov, "AVISO: Sem email de contacto — notificação omitida.")
		return

	reset_link = _generate_password_reset_link(prov)
	_send_welcome_email(prov, reset_link)
	_append_log(prov, f"Email de boas-vindas enviado para {prov.contact_email}.")


# ---------------------------------------------------------------------------
# Password reset link
# ---------------------------------------------------------------------------

def _generate_password_reset_link(prov) -> str:
	"""Generate a one-time password-reset URL for the contact_email user on the new site.

	Uses `bench execute` with `__import__` so the function is resolved at eval time
	inside the new site's Frappe context. The plain dotted-path form fails because
	bench execute evals in utils.py's global namespace where only `frappe` is
	imported; `__import__` is a builtin and always in scope.

	The key is stored for `prov.contact_email` — the user created by the setup wizard,
	NOT Administrator.
	"""
	method = (
		"__import__('ai_saas.saas.site_helpers', fromlist=['generate_user_reset_link'])"
		".generate_user_reset_link"
	)
	raw = _run_cmd_capture(
		[
			_get_bench_cmd(), "--site", prov.site_name, "execute", method,
			"--kwargs", json.dumps({"email": prov.contact_email}),
		],
		step="reset-key",
		prov=prov,
		timeout=30,
	)
	# bench execute JSON-encodes the return value on stdout.
	try:
		url = json.loads(raw.strip())
	except (json.JSONDecodeError, ValueError):
		raise ProvisioningError(f"Link de reset inválido retornado pelo site: {raw!r}")
	return url


# ---------------------------------------------------------------------------
# subprocess wrapper
# ---------------------------------------------------------------------------

def _run_cmd(cmd: list, step: str, prov, timeout: int) -> None:
	"""Run a bench command as a subprocess. Raises ProvisioningError on failure.

	Uses shell=False (list form) — credentials in cmd args cannot cause shell injection.
	"""
	try:
		result = subprocess.run(
			cmd,
			capture_output=True,
			text=True,
			cwd=_get_bench_path(),
			env={**os.environ},
			timeout=timeout,
		)
	except subprocess.TimeoutExpired as exc:
		raise ProvisioningError(f"Passo '{step}' expirou ao fim de {timeout}s") from exc
	except OSError as exc:
		raise ProvisioningError(f"Passo '{step}' falhou ao iniciar: {exc}") from exc

	output = (result.stdout or "") + (result.stderr or "")
	_append_log(prov, f"[{step}] saída={result.returncode}\n{output[:4000]}")

	if result.returncode != 0:
		raise ProvisioningError(
			f"Passo '{step}' terminou com código {result.returncode}. "
			f"Últimas linhas: {output[-500:]}"
		)


def _run_cmd_capture(cmd: list, step: str, prov, timeout: int) -> str:
	"""Like _run_cmd but returns stdout for commands whose output we need to read."""
	try:
		result = subprocess.run(
			cmd,
			capture_output=True,
			text=True,
			cwd=_get_bench_path(),
			env={**os.environ},
			timeout=timeout,
		)
	except subprocess.TimeoutExpired as exc:
		raise ProvisioningError(f"Passo '{step}' expirou ao fim de {timeout}s") from exc
	except OSError as exc:
		raise ProvisioningError(f"Passo '{step}' falhou ao iniciar: {exc}") from exc

	output = (result.stdout or "") + (result.stderr or "")
	_append_log(prov, f"[{step}] saída={result.returncode}\n{output[:4000]}")

	if result.returncode != 0:
		raise ProvisioningError(
			f"Passo '{step}' terminou com código {result.returncode}. "
			f"Últimas linhas: {output[-500:]}"
		)
	return result.stdout or ""


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

def _handle_failure(prov, error_message: str) -> None:
	_append_log(prov, f"ERRO: {error_message}")
	prov.last_error = frappe.get_traceback()
	prov.status = "Failed"
	prov.save(ignore_permissions=True)
	frappe.db.commit()

	_send_failure_alert(prov, error_message)

	if prov.attempts < MAX_ATTEMPTS:
		frappe.enqueue(
			"ai_saas.saas.provisioning._run_provisioning",
			queue="long",
			timeout=PROVISION_TIMEOUT + 120,
			job_id=f"provision-tenant-{prov.name}-retry-{prov.attempts}",
			enqueue_after_commit=True,
			provisioning_name=prov.name,
		)
	else:
		# All retries exhausted — inform the customer
		if prov.contact_email:
			frappe.sendmail(
				recipients=[prov.contact_email],
				subject="Problema temporário com a sua conta MozEconomia Cloud",
				message=(
					f"<p>Prezado(a) {prov.customer_name},</p>"
					f"<p>Encontrámos um problema técnico ao preparar a sua instância. "
					f"A nossa equipa foi notificada e entrará em contacto em breve.</p>"
					f"<p>Pedimos desculpa pelo inconveniente.</p>"
					f"<p><strong>Equipa MozEconomia Cloud</strong></p>"
				),
				delayed=False,
			)


# ---------------------------------------------------------------------------
# Email helpers
# ---------------------------------------------------------------------------

_LOGO_URL = "https://raw.githubusercontent.com/bboa3/logos/refs/heads/main/mozeconomia-logo.png"


def _send_welcome_email(prov, reset_link: str) -> None:
	url = f"https://{prov.site_name}"
	message = f"""
<table cellpadding="0" cellspacing="0" border="0" width="100%"
       style="background:#F5F6F7;padding:20px 0;font-family:Arial,sans-serif;color:#1e2a38;line-height:1.5;">
  <tr>
    <td align="center">
      <table cellpadding="0" cellspacing="0" border="0" width="600"
             style="background:#FFFFFF;border-radius:8px;overflow:hidden;mso-table-lspace:0;mso-table-rspace:0;border-collapse:collapse;">
        <tr>
          <td style="padding:30px;font-size:14px;color:#1e2a38;">
            <p>Prezado(a) {prov.customer_name},</p>
            <p><strong>O seu sistema MozEconomia Cloud está activo!</strong></p>
            <p>
              A instância da <strong>{prov.customer_name}</strong> foi criada com sucesso e está pronta a
              utilizar em: <a href="{url}" style="color:#008000;font-weight:bold;">{prov.site_name}</a>
            </p>
            <p>Para aceder ao sistema pela primeira vez, clique no botão abaixo para definir a sua senha:</p>
            <p style="margin:24px 0;">
              <a href="{reset_link}"
                 style="background:#008000;color:#ffffff;padding:12px 28px;border-radius:5px;
                        text-decoration:none;font-size:15px;font-weight:bold;display:inline-block;">
                Definir a Minha Senha
              </a>
            </p>
            <p style="font-size:12px;color:#6c757d;">
              Se o botão não funcionar, copie e cole este link no seu browser:<br>
              <a href="{reset_link}" style="color:#008000;">{reset_link}</a>
            </p>
            <p><strong>O que acontece a seguir:</strong></p>
            <ol>
              <li>A nossa equipa vai contactar nas próximas horas para agendar a sessão de configuração inicial</li>
              <li>Juntos vamos configurar a sua empresa, produtos e utilizadores</li>
              <li>A sua equipa começa a faturar, gerir stock e finanças desde o primeiro dia</li>
            </ol>
            <p>As suas faturas mensais chegam a este email com <strong>7 dias de prazo para pagamento</strong>.</p>
            <p>
              Qualquer dúvida, basta escrever para
              <a href="mailto:cloud@mozeconomia.co.mz" style="color:#008000;">cloud@mozeconomia.co.mz</a>
              ou falar connosco pelo WhatsApp.
            </p>
            <p>Com boas energias,</p>
          </td>
        </tr>
        <tr>
          <td style="padding:0 30px 30px 30px;">
            <table cellpadding="0" cellspacing="0" border="0" width="100%"
                   style="font-family:Arial,sans-serif;font-size:14px;color:#1e2a38;mso-table-lspace:0;mso-table-rspace:0;border-collapse:collapse;">
              <tr>
                <td style="padding:0 0 8px 0;">
                  <img src="{_LOGO_URL}" alt="MozEconomia Cloud" width="240"
                       style="display:block;border:0;outline:none;text-decoration:none;">
                </td>
              </tr>
              <tr>
                <td style="padding:4px 0;">
                  <table cellpadding="0" cellspacing="0" border="0" width="100%"
                         style="mso-table-lspace:0;mso-table-rspace:0;border-collapse:collapse;">
                    <tr>
                      <td valign="top" style="font-weight:bold;width:80px;padding:2px 0;">Telefone:</td>
                      <td style="padding:2px 0;">
                        <a href="tel:+258874444645" style="color:#1e2a38;text-decoration:none;">+258 87 4444 645</a>
                      </td>
                    </tr>
                    <tr>
                      <td valign="top" style="font-weight:bold;padding:2px 0;">Email:</td>
                      <td style="padding:2px 0;">
                        <a href="mailto:cloud@mozeconomia.co.mz" style="color:#1e2a38;text-decoration:none;">cloud@mozeconomia.co.mz</a>
                      </td>
                    </tr>
                    <tr>
                      <td valign="top" style="font-weight:bold;padding:2px 0;">Website:</td>
                      <td style="padding:2px 0;">
                        <a href="https://mozeconomia.co.mz" style="color:#1e2a38;text-decoration:none;">mozeconomia.co.mz</a>
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>
              <tr>
                <td style="padding:8px 0 0 0;font-size:12px;color:#1e2a38;">
                  MOZ ECONOMIA S.A. • Soluções de contabilidade e gestão empresarial.
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
"""
	frappe.sendmail(
		recipients=[prov.contact_email],
		subject="Bem-vindo à MozEconomia Cloud — A sua conta está activa!",
		message=message,
		reference_doctype="MZ Tenant Provisioning",
		reference_name=prov.name,
		delayed=False,
	)


def _send_failure_alert(prov, error_message: str) -> None:
	support_email = (
		frappe.db.get_value("User", {"name": "Administrator"}, "email")
		or "contacto@mozeconomia.co.mz"
	)
	frappe.sendmail(
		recipients=[support_email],
		subject=f"[ALERTA] Falha no provisionamento: {prov.site_name}",
		message=(
			f"<p>O provisionamento automático para <strong>{prov.site_name}</strong> falhou.</p>"
			f"<p><strong>Tentativa:</strong> {prov.attempts} de {MAX_ATTEMPTS}</p>"
			f"<p><strong>Erro:</strong></p><pre>{error_message[:2000]}</pre>"
			f"<p>Ver registo: MZ Tenant Provisioning / {prov.name}</p>"
		),
		reference_doctype="MZ Tenant Provisioning",
		reference_name=prov.name,
		delayed=False,
	)


# ---------------------------------------------------------------------------
# Validation & utilities
# ---------------------------------------------------------------------------

def _resolve_contact_email(contract) -> str:
	"""Return the contact email for a contract from the Customer record.

	Uses the same lookup order as _collect_customer_profile:
	1. Customer.email_id (direct field, populated from primary contact)
	2. Primary Contact linked to the Customer (Dynamic Link)
	The explicit contract.contact_email field overrides both when set.
	"""
	# Explicit override on the contract takes highest priority
	explicit = (contract.get("contact_email") or "").strip()
	if explicit:
		return explicit

	customer_name = contract.party_name

	# Customer.email_id — same first source as _collect_customer_profile
	email = (frappe.db.get_value("Customer", customer_name, "email_id") or "").strip()
	if email:
		return email

	# Primary Contact linked to the Customer — same fallback as _collect_customer_profile
	contact_name = frappe.db.get_value(
		"Dynamic Link",
		{"link_doctype": "Customer", "link_name": customer_name, "parenttype": "Contact"},
		"parent",
	)
	if contact_name:
		email = (frappe.db.get_value("Contact", contact_name, "email_id") or "").strip()

	return email


def _validate_slug(slug: str) -> None:
	if not SLUG_RE.match(slug):
		frappe.throw(
			f"Subdomínio inválido: '{slug}'. Use apenas letras minúsculas, números e hífens. "
			f"Mínimo 3 e máximo 40 caracteres. Não pode começar nem terminar com hífen.",
			frappe.ValidationError,
		)
	if slug in RESERVED_SLUGS:
		frappe.throw(
			f"O subdomínio '{slug}' está reservado e não pode ser utilizado.",
			frappe.ValidationError,
		)


def _generate_admin_password() -> str:
	alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
	while True:
		pwd = "".join(secrets.choice(alphabet) for _ in range(20))
		if (
			any(c.isupper() for c in pwd)
			and any(c.islower() for c in pwd)
			and any(c.isdigit() for c in pwd)
			and any(c in "!@#$%^&*" for c in pwd)
		):
			return pwd


def _make_company_abbr(company_name: str) -> str:
	words = company_name.split()
	abbr = "".join(w[0].upper() for w in words if w)[:5]
	return abbr or "EMP"


def _get_db_root_password() -> str:
	password = frappe.conf.get("db_root_password")
	if not password:
		raise ProvisioningError(
			"Senha root do MariaDB não configurada. "
			"Adicione 'db_root_password' ao common_site_config.json."
		)
	return password


def _get_site_admin_password() -> str:
	"""Internal password used only for bench new-site. Never sent to customers."""
	password = frappe.conf.get("ai_saas_admin_password")
	if not password:
		raise ProvisioningError(
			"Senha de administração do site não configurada. "
			"Adicione 'ai_saas_admin_password' ao common_site_config.json."
		)
	return password


def _get_contact_password(prov) -> str:
	"""Return the plaintext contact user password stored in the provisioning record.

	prov.contact_password after frappe.get_doc() is the Frappe-masked value '***'.
	get_decrypted_password() fetches the real plaintext from the __Auth table.
	"""
	return get_decrypted_password("MZ Tenant Provisioning", prov.name, "contact_password")


def _append_log(prov, message: str) -> None:
	prov.log = (prov.log or "") + f"[{frappe.utils.now()}] {message}\n"
