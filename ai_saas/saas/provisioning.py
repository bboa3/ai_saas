import json
import os
import re
import secrets
import string
import subprocess

import frappe
from frappe.utils.password import get_decrypted_password

from ai_saas.saas.alerts import notify_ops, ops_alert_recipients  # noqa: F401 — re-exported for callers

_DEFAULT_BENCH_PATH = "/home/frappe/frappe-bench"
_DEFAULT_BENCH_CMD = "/usr/local/bin/bench"


def get_bench_path() -> str:
	return frappe.conf.get("bench_path") or _DEFAULT_BENCH_PATH


def get_bench_cmd() -> str:
	return frappe.conf.get("bench_cmd") or _DEFAULT_BENCH_CMD
DOMAIN_SUFFIX = ".erp.mozeconomia.co.mz"
MAX_ATTEMPTS = 3
PROVISION_TIMEOUT = 1200  # 20 min — bench new-site + app installs
WIZARD_TIMEOUT = 300

# Fallback used only when the Contract has no apps configured
# The apps a tenant gets when the contract names none. Single source of truth:
# install.py renders it into the Contract client script, api/signup.py fills it in.
DEFAULT_APPS = ["erpnext", "hrms", "erpnext_mz", "pos_next", "payments"]

# The setup wizard resolves its language argument with its own get_language_code(),
# which looks the Language up by `language_name` and nothing else.  Handing it the
# code returns None and the wizard falls back to "en", so it gets the name.
MZ_LANGUAGE_CODE = "pt-MZ"
MZ_LANGUAGE_NAME = "Português (Moçambique)"

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

	existing = frappe.db.get_value(
		"MZ Tenant Provisioning", {"contract": contract_name}, ["name", "status"], as_dict=True
	)
	if existing:
		# One record per contract for life — signing later can never create a second
		# site. The only state that gets a second chance is Failed (C1): a re-submitted
		# or re-signed contract re-queues it; every other status keeps the silent return.
		if existing.status == "Failed":
			retry_failed_provisioning(existing.name, trigger=f"contrato {contract_name} re-processado")
		return

	validate_slug(slug)
	# Two contracts must never share a slug: the second's setup wizard would run
	# against the first customer's site. Surfaced to the user like an invalid slug.
	holder = frappe.db.get_value("MZ Tenant Provisioning", {"tenant_slug": slug}, "contract")
	if holder and holder != contract_name:
		frappe.throw(
			f"O subdomínio '{slug}' já está atribuído ao contrato {holder}.", frappe.ValidationError
		)

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
		for step in PROVISIONING_STEPS:
			step(prov)

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

	site_dir = os.path.join(get_bench_path(), "sites", prov.site_name)
	if os.path.exists(site_dir):
		_append_log(prov, "Diretório do site já existe — a saltar bench new-site (tentativa anterior).")
		return

	db_root_user = get_db_root_user()
	db_root_password = get_db_root_password()
	site_admin_password = _get_site_admin_password()

	apps = [row.app_name for row in (prov.get("mz_provisioning_apps") or []) if row.app_name]
	if not apps:
		apps = list(DEFAULT_APPS)

	install_app_args = []
	for app in apps:
		install_app_args += ["--install-app", app]

	run_cmd(
		[
			get_bench_cmd(), "new-site", prov.site_name,
			"--db-root-username", db_root_user,
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
	run_cmd(
		[get_bench_cmd(), "--site", prov.site_name, "set-config", "host_name", f"https://{prov.site_name}"],
		step="set-hostname",
		prov=prov,
		timeout=15,
	)
	_append_log(prov, f"Site {prov.site_name} criado, aplicações instaladas e host_name configurado.")


def _step_ensure_language(prov) -> None:
	"""Create the pt-MZ Language record before the wizard runs.

	The wizard resolves its language argument by `language_name`, so the record has to
	exist by then or the lookup returns None and the wizard writes "en".  `after_install`
	already creates it; this is the guard for a site whose app install predates that.
	"""
	_append_log(prov, "A garantir registo de idioma pt-MZ no novo site")
	run_cmd(
		[
			get_bench_cmd(), "--site", prov.site_name,
			"execute",
			"erpnext_mz.setup.language.ensure_language_pt_mz",
		],
		step="ensure-language",
		prov=prov,
		timeout=30,
	)


def build_wizard_args(prov) -> dict:
	"""The arguments handed to Frappe's setup_complete().

	`language` is what `update_system_settings` reads, `lang` what `update_global_settings`
	reads -- and both go through the wizard's own `get_language_code()`, which matches on
	`language_name` alone.  Passing the code "pt-MZ" resolves to None and the wizard writes
	"en", which is what put a Portuguese tenant on an English desk.  Omitting `lang`
	separately makes `update_global_settings` call `set_default_language(None)`.
	"""
	year = frappe.utils.getdate().year
	return {
		"country": "Mozambique",
		"language": MZ_LANGUAGE_NAME,
		"lang": MZ_LANGUAGE_NAME,
		"timezone": "Africa/Maputo",
		"currency": "MZN",
		"email": prov.contact_email or f"admin@{prov.site_name}",
		"full_name": prov.customer_name,
		"password": _get_contact_password(prov),
		"company_name": prov.customer_name,
		"company_abbr": _make_company_abbr(prov.customer_name),
		"fy_start_date": f"{year}-01-01",
		"fy_end_date": f"{year}-12-31",
	}


def _step_run_setup_wizard(prov) -> None:
	prov.status = "Running Setup Wizard"
	_append_log(prov, "A executar o assistente de configuração")
	prov.save(ignore_permissions=True)
	frappe.db.commit()

	wizard_kwargs = build_wizard_args(prov)

	# setup_complete(args) takes a single positional parameter.
	# bench execute --kwargs expands kwargs as **kwargs, so we must nest the
	# wizard data under the key "args" so bench calls setup_complete(args={...}).
	run_cmd(
		[
			get_bench_cmd(), "--site", prov.site_name,
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
	"""Apply the Mozambique system defaults AFTER the wizard.

	This used to run before it, on the reasoning that the wizard would inherit them.  It
	does not: `update_system_settings` writes country, currency, time zone, language,
	date and number format, float precision and rounding method from its own arguments
	and from the Country record, overwriting whatever was there.  Anything set before the
	wizard that the wizard also writes is lost -- the language among it, which is how a
	tenant provisioned in Portuguese ended up with an English desk.
	"""
	_append_log(prov, "A aplicar definições de sistema (idioma pt-MZ, MZN, fuso horário)")
	run_cmd(
		[
			get_bench_cmd(), "--site", prov.site_name,
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

	run_cmd(
		[
			get_bench_cmd(), "--site", prov.site_name,
			"execute",
			"erpnext_mz.setup.onboarding.seed_company_profile",
			"--kwargs", json.dumps({"data": data}),
		],
		step="seed-company-profile",
		prov=prov,
		timeout=60,
	)
	_append_log(prov, f"Perfil pré-preenchido: {', '.join(data.keys())}")


def _structured_address(addr, contract_name) -> dict:
	"""Address parts for the tenant profile: the Address record's own fields, with the
	gaps filled by parsing whatever one-line text exists (Address.address_line1, else
	the signup's typed address). Keys: address_line1, neighborhood_or_district, city, province."""
	from ai_saas.saas.mz_address import parse_mz_address

	out = {}
	if addr:
		if addr.address_line1:
			out["address_line1"] = addr.address_line1
		if addr.address_line2:
			out["neighborhood_or_district"] = addr.address_line2
		if addr.city:
			out["city"] = addr.city
		if addr.state:
			out["province"] = addr.state

	if not out.get("city"):
		# The city the visitor answered on the form when the typed line had none.
		out["city"] = frappe.db.get_value("MZ Signup", {"contract": contract_name}, "city") or ""
		if not out["city"]:
			out.pop("city", None)

	needs_parsing = not (out.get("city") and out.get("address_line1"))
	one_line = (addr.address_line1 if addr and addr.address_line1 else "") or ""
	if not one_line:
		one_line = frappe.db.get_value("MZ Signup", {"contract": contract_name}, "address") or ""
	if needs_parsing and one_line:
		parsed = parse_mz_address(", ".join(p for p in (one_line, addr.address_line2 if addr else "") if p))
		for src, dst in (("address_line1", "address_line1"), ("address_line2", "neighborhood_or_district"),
		                 ("city", "city"), ("state", "province")):
			if parsed.get(src) and not out.get(dst):
				out[dst] = parsed[src]
	return out


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
		addr = None
		if address_name:
			addr = frappe.db.get_value(
				"Address",
				address_name,
				["address_line1", "address_line2", "city", "state", "phone"],
				as_dict=True,
			)
		if addr and addr.phone and not data.get("phone"):
			data["phone"] = addr.phone

		# The tenant's company profile wants structured parts (line, bairro, city,
		# province). Wherever the address arrived as one line — a sales user typing the
		# whole thing into address_line1, a Customer with no Address but a signup that
		# captured one — the same parser used at signup normalises it. Structured parts
		# already on the Address always win; the parser only fills what is missing.
		data.update(_structured_address(addr, prov.contract))

		return data
	except Exception:
		frappe.log_error(frappe.get_traceback(), "AI SaaS: _collect_customer_profile failed")
		return {}


# ---------------------------------------------------------------------------
# Scheduler policy: background jobs are a Premium feature
# ---------------------------------------------------------------------------

SCHEDULER_TIMEOUT = 60


def scheduler_enabled_for_plan(plan_name) -> bool:
	"""The tenant's scheduler is on when the contract's plan is listed in
	MZ SaaS Settings.scheduler_plans — the one place the team decides which plans
	include background jobs. No plan, or a plan not listed, means no scheduler."""
	if not plan_name:
		return False
	from ai_saas.saas.tenant_lifecycle import get_settings

	return plan_name in get_settings().scheduler_plans


def scheduler_enabled_for_contract(contract_name) -> bool:
	return scheduler_enabled_for_plan(frappe.db.get_value("Contract", contract_name, "mz_subscription_plan"))


def _apply_scheduler_state(prov, enabled: bool, trigger: str = "provisionamento") -> None:
	"""`bench enable-scheduler` / `disable-scheduler` write System Settings.enable_scheduler
	on the tenant — the switch frappe.utils.scheduler.is_scheduler_disabled reads."""
	_append_log(prov, f"Agendador do site: {'ACTIVADO' if enabled else 'DESACTIVADO'} pelo plano do contrato ({trigger})")
	run_cmd(
		[get_bench_cmd(), "--site", prov.site_name, "enable-scheduler" if enabled else "disable-scheduler"],
		f"scheduler-policy ({trigger})", prov, SCHEDULER_TIMEOUT,
	)


def _step_apply_scheduler_policy(prov) -> None:
	"""After the wizard and system settings (either could touch enable_scheduler):
	the plan decides. Explicit both ways — Frappe's default is not a policy."""
	_apply_scheduler_state(prov, scheduler_enabled_for_contract(prov.contract))


def apply_scheduler_policy(contract_name) -> bool:
	"""Re-apply the plan's scheduler policy to a live site — called at signature,
	because the plan may have been corrected at activation. Returns the state set.
	Raises ProvisioningError if the bench command fails; no-op without a live site."""
	prov = frappe.db.get_value(
		"MZ Tenant Provisioning", {"contract": contract_name, "status": ("in", ["Active", "Suspended"])}, "name"
	)
	if not prov:
		return False
	prov = frappe.get_doc("MZ Tenant Provisioning", prov)
	enabled = scheduler_enabled_for_contract(contract_name)
	_apply_scheduler_state(prov, enabled, trigger="assinatura")
	frappe.db.set_value("MZ Tenant Provisioning", prov.name, "log", prov.log, update_modified=False)
	return enabled


def _step_setup_smtp(prov) -> None:
	"""Configure outbound email on the new site so the customer can receive password-reset
	emails after the initial reset link expires. Non-fatal: logs a warning on failure so
	that a misconfigured SMTP server never blocks the provisioning from completing."""
	_append_log(prov, "A configurar email no novo site")
	try:
		run_cmd(
			[
				get_bench_cmd(), "--site", prov.site_name,
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
	raw = run_cmd_capture(
		[
			get_bench_cmd(), "--site", prov.site_name, "execute", method,
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

def run_cmd(cmd: list, step: str, prov, timeout: int) -> None:
	"""Run a bench command as a subprocess. Raises ProvisioningError on failure.

	Uses shell=False (list form) — credentials in cmd args cannot cause shell injection.
	"""
	try:
		result = subprocess.run(
			cmd,
			capture_output=True,
			text=True,
			cwd=get_bench_path(),
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


def run_cmd_capture(cmd: list, step: str, prov, timeout: int) -> str:
	"""Like _run_cmd but returns stdout for commands whose output we need to read."""
	try:
		result = subprocess.run(
			cmd,
			capture_output=True,
			text=True,
			cwd=get_bench_path(),
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

def retry_failed_provisioning(provisioning_name: str, trigger: str = "manual") -> None:
	"""Give a Failed provisioning record its attempts back and re-queue it (C1).

	Safe against a half-created site: _step_create_site skips `bench new-site`
	when the site directory already exists, so the retry resumes rather than
	collides. Any other status is refused — this is not a general re-run button.
	"""
	prov = frappe.get_doc("MZ Tenant Provisioning", provisioning_name)
	if prov.status != "Failed":
		frappe.throw(
			f"Só um provisionamento em estado 'Failed' pode ser repetido — "
			f"{prov.name} está '{prov.status}'."
		)
	_append_log(prov, f"Nova tentativa ({trigger}): contador de tentativas reposto a 0")
	prov.attempts = 0
	prov.status = "Queued"
	prov.last_error = ""
	prov.save(ignore_permissions=True)
	frappe.db.commit()

	frappe.enqueue(
		"ai_saas.saas.provisioning._run_provisioning",
		queue="long",
		timeout=PROVISION_TIMEOUT + 120,
		job_id=f"provision-tenant-{prov.name}-retry-{frappe.utils.now_datetime().strftime('%Y%m%d%H%M%S')}",
		enqueue_after_commit=True,
		provisioning_name=prov.name,
	)


@frappe.whitelist()
def retry_provisioning(name: str) -> str:
	"""The "Tentar Novamente" button on MZ Tenant Provisioning — A6's one-click answer."""
	if not frappe.has_permission("MZ Tenant Provisioning", "write", doc=name):
		frappe.throw("Sem permissão para repetir o provisionamento.", frappe.PermissionError)
	retry_failed_provisioning(name, trigger=f"botão, utilizador {frappe.session.user}")
	return "queued"


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
	# When retries are exhausted nothing more is sent: the lead is never told that provisioning
	# failed — the team alert above (sent on every attempt) is what triggers the human fix, and the
	# lead simply receives the delivery email once "Tentar Novamente" succeeds.


# ---------------------------------------------------------------------------
# Email helpers
# ---------------------------------------------------------------------------

def _welcome_email_context(prov, reset_link: str) -> dict:
	"""Context the delivery Email Template renders with (C2)."""
	from ai_saas.saas.activation import get_activation_url
	from ai_saas.utils.jinja import mz_first_name, mz_greeting, mz_signature

	contract = frappe.db.get_value(
		"Contract", prov.contract,
		["is_signed", "start_date", "mz_subscription_plan", "mz_contact_name", "mz_account_manager"], as_dict=True,
	) or frappe._dict()
	return {
		"customer_name": prov.customer_name,
		"first_name": mz_first_name(contract.get("mz_contact_name")),
		"greeting": mz_greeting(contract.get("mz_contact_name")),
		"signature": mz_signature(contract.get("mz_account_manager")),
		"contact_email": prov.contact_email,
		"site_name": prov.site_name,
		"site_url": f"https://{prov.site_name}",
		"reset_link": reset_link,
		"is_signed": bool(contract.get("is_signed")),
		"trial_end": frappe.utils.formatdate(contract.get("start_date")) if contract.get("start_date") else "",
		"plan": contract.get("mz_subscription_plan") or "",
		"activation_url": get_activation_url(prov.contract),
		"booking_url": get_booking_url(),
	}


def get_booking_url() -> str:
	"""The Calendly link from MZ SaaS Settings (install seeds it); the shipped default
	if someone blanks the setting — an email must never carry an empty link."""
	from ai_saas.install import DEFAULT_BOOKING_URL

	return frappe.db.get_single_value("MZ SaaS Settings", "booking_url") or DEFAULT_BOOKING_URL


def _render_welcome_email(prov, reset_link: str) -> dict:
	"""subject + message from the Email Template; raises if the template is missing
	(install.ensure_email_templates creates it on install and migrate)."""
	from ai_saas.install import WELCOME_EMAIL_TEMPLATE

	template = frappe.get_doc("Email Template", WELCOME_EMAIL_TEMPLATE)
	return template.get_formatted_email(_welcome_email_context(prov, reset_link))


def _send_welcome_email(prov, reset_link: str) -> None:
	email = _render_welcome_email(prov, reset_link)
	frappe.sendmail(
		recipients=[prov.contact_email],
		subject=email["subject"],
		message=email["message"],
		reference_doctype="MZ Tenant Provisioning",
		reference_name=prov.name,
		delayed=False,
	)


def _send_failure_alert(prov, error_message: str) -> None:
	signup = frappe.db.get_value("MZ Signup", {"contract": prov.contract}, "name")
	exhausted = prov.attempts >= MAX_ATTEMPTS
	urgency = (
		"<p style='color:#c0392b'><strong>Tentativas esgotadas — não há mais repetições automáticas. "
		"O lead NÃO foi informado; está à espera da conta. Corrija e use «Tentar Novamente» já.</strong></p>"
		if exhausted
		else "<p>Será repetido automaticamente. O lead não foi informado.</p>"
	)
	notify_ops(
		f"{'URGENTE — ' if exhausted else ''}Falha no provisionamento: {prov.site_name}",
		f"<p>O provisionamento automático para <strong>{prov.site_name}</strong> falhou.</p>"
		f"<p><strong>Tentativa:</strong> {prov.attempts} de {MAX_ATTEMPTS}</p>"
		+ urgency +
		f"<p><strong>Erro:</strong></p><pre>{frappe.utils.escape_html(error_message[:2000])}</pre>"
		f"<p>Ver registo: MZ Tenant Provisioning / {prov.name} · Contrato {prov.contract}</p>"
		+ (f"<p>Registo de auto-serviço: MZ Signup / {signup}</p>" if signup else ""),
		reference_doctype="MZ Tenant Provisioning",
		reference_name=prov.name,
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


def validate_slug(slug: str) -> None:
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


def get_db_root_user() -> str:
	"""MariaDB account used to create the tenant database and its user.

	Defaults to 'root', but on Debian/Ubuntu root@localhost authenticates via the
	unix_socket plugin and rejects password logins with error 1698 — so a dedicated
	password-authenticated account must be used instead. Set 'db_root_user' in
	common_site_config.json to override.
	"""
	return frappe.conf.get("db_root_user") or "root"


def get_db_root_password() -> str:
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


# ---------------------------------------------------------------------------
# The provisioning sequence
# ---------------------------------------------------------------------------

# Order matters twice over.  The Language record has to exist before the wizard, which
# resolves its language argument by `language_name`.  And the system defaults have to be
# applied after it, because `update_system_settings` overwrites language, country,
# currency, time zone, date and number format with its own values.
PROVISIONING_STEPS = (
	_step_create_site,
	_step_ensure_language,
	_step_run_setup_wizard,
	_step_apply_system_settings,
	_step_seed_company_profile,
	_step_apply_scheduler_policy,
	_step_setup_smtp,
	_step_notify_customer,
)


# Backward-compatible aliases (tests and older call sites patch these names).
_get_bench_path = get_bench_path
_get_bench_cmd = get_bench_cmd
_validate_slug = validate_slug
_run_cmd = run_cmd
_run_cmd_capture = run_cmd_capture
_get_db_root_user = get_db_root_user
_get_db_root_password = get_db_root_password
_ops_alert_recipients = ops_alert_recipients
