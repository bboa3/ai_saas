"""Tenant lifecycle engine (docs/sales-funnel-implementation.md, F1-F6).

Three idempotent operations — suspend, reactivate, archive — plus the daily job
that applies the two switch-off rules and the archive rule. The engine reads
dates, never counts days, and sees ONLY contracts with a provisioning record:
the account's phase is derived (account_phase) from the signature and the site's
status — a commercial contract without a site is invisible.

Two switches in MZ SaaS Settings arm the daily job — auto_suspend (reversible)
and auto_archive (irreversible) — both off by default and when never configured.
Whatever is not armed is still evaluated and reported in the daily digest to the
ops recipients, so the rules can be watched on real data before they act. Manual
actions from the review queue always execute. Suspension is `bench set-maintenance-mode on`
— every HTTP request answers 503 pre-auth and the tenant's scheduler stops, but
`bench execute` (the usage probe) keeps working. Data is untouched and one
command reverses it. Archive is the one irreversible act: full backup, verify,
then `bench drop-site`, which backs up again and moves the site directory to
<bench>/archived/sites/.
"""

import os
import time

import frappe
from frappe.utils import add_days, add_to_date, cint, getdate, now_datetime, nowdate

from ai_saas.saas import crm
from ai_saas.saas.alerts import notify_ops
from ai_saas.saas.lifecycle_mail import send_lifecycle_email
from ai_saas.saas.provisioning import (
	ProvisioningError,
	get_bench_cmd,
	get_bench_path,
	get_db_root_password,
	get_db_root_user,
	run_cmd,
)
from ai_saas.saas.settings import get_settings

ARCHIVE_TIMEOUT = 1800  # backup + drop-site of a full tenant site
MAINTENANCE_TIMEOUT = 60


# ---------------------------------------------------------------------------
# The three operations
# ---------------------------------------------------------------------------

def suspend(contract_name, reason="", cause="manual", invoice=None, notify=True):
	"""Block access to the contract's site. Data intact, fully reversible.
	`cause` ("trial" | "overdue" | "manual") and `invoice` shape the customer's email."""
	prov = _get_prov(contract_name)
	if prov.status == "Suspended":
		return
	if prov.status != "Active":
		# Queued / Creating Site / Failed / Archived: there is no live site to darken.
		frappe.throw(
			f"O site do contrato {contract_name} não está activo (estado '{prov.status}') — nada a suspender."
		)

	_set_maintenance_mode(prov, on=True)
	frappe.db.set_value(
		"MZ Tenant Provisioning",
		prov.name,
		{"status": "Suspended", "suspended_on": now_datetime()},
	)
	_log(prov, f"Suspenso. Motivo: {reason}")
	crm.report_for_contract(
		contract_name, crm.STAGE_TRIAL_EXPIRED if cause == "trial" else crm.STAGE_SUSPENDED
	)
	if notify:
		send_lifecycle_email("suspended", contract_name, cause=cause, invoice=invoice or "")


def reactivate(contract_name, new_start_date=None, force=False, notify=True):
	"""Restore access. An unsigned trial MUST get a new start_date, or the
	engine's first rule simply suspends it again on the next daily run. A signed
	contract suspended for non-payment needs the debt settled — or force=True,
	the review queue's explicit human decision — or Rule 2 re-suspends it tomorrow."""
	prov = _get_prov(contract_name)
	if prov.status == "Archived":
		frappe.throw(
			f"O site do contrato {contract_name} foi arquivado — a reactivação é um restauro "
			f"manual a partir do backup em: {prov.backup_path or 'localização não registada'}."
		)

	contract = frappe.db.get_value(
		"Contract", contract_name, ["is_signed", "start_date"], as_dict=True
	)
	is_trial = not contract.is_signed
	if not is_trial and not force and _has_overdue_invoice(contract_name):
		frappe.throw(
			"Há facturas em atraso ligadas a este contrato — o motor voltaria a suspender amanhã. "
			"Regularize a dívida, ou reactive a partir da fila de revisão (decisão explícita)."
		)
	if is_trial and getdate(contract.start_date) <= getdate(nowdate()) and not new_start_date:
		frappe.throw(
			"Reactivar um trial não assinado exige uma nova data de fim do trial "
			"(start_date do contrato) — sem ela o motor volta a suspendê-lo amanhã."
		)

	_set_maintenance_mode(prov, on=False)
	if is_trial and new_start_date:
		frappe.db.set_value("Contract", contract_name, "start_date", getdate(new_start_date))
	frappe.db.set_value(
		"MZ Tenant Provisioning", prov.name, {"status": "Active", "suspended_on": None}
	)
	_log(prov, f"Reactivado. Nova data de fim do trial: {new_start_date or '—'}")
	crm.report_for_contract(contract_name, crm.STAGE_ACCOUNT_CREATED if is_trial else crm.STAGE_ACTIVATED)
	if notify:
		send_lifecycle_email("reactivated", contract_name, new_trial_end=new_start_date or "")


def archive(contract_name, notify=True):
	"""Full backup, verify, destroy. The one irreversible act — triple-gated:
	the site must be Suspended, the grace period elapsed (checked by the caller
	— the daily rule), and the backup verified before drop-site runs."""
	prov = _get_prov(contract_name)
	if prov.status == "Archived":
		return
	if prov.status != "Suspended":
		frappe.throw(
			f"Só um site suspenso pode ser arquivado — {prov.site_name} está '{prov.status}'."
		)
	site = prov.site_name
	bench = get_bench_cmd()

	# 1. Our own full backup, before drop-site's one — belt and braces.
	_run(prov, [bench, "--site", site, "backup", "--with-files"], "archive-backup", ARCHIVE_TIMEOUT)
	backups_dir = os.path.join(get_bench_path(), "sites", site, "private", "backups")
	if not _has_recent_backup(backups_dir):
		raise ProvisioningError(f"Backup não confirmado em {backups_dir} — drop-site cancelado.")

	# 2. drop-site: backs up again by default and MOVES the site directory
	#    (with both backups inside) to <bench>/archived/sites/<site>.
	drop_cmd = [bench, "drop-site", site, "--db-root-password", get_db_root_password(),
	            "--db-root-username", get_db_root_user()]
	_run(prov, drop_cmd, "drop-site", ARCHIVE_TIMEOUT)

	archived_path = os.path.join(get_bench_path(), "archived", "sites", site)
	if not os.path.isdir(archived_path):
		raise ProvisioningError(
			f"drop-site terminou mas {archived_path} não existe — verificar manualmente."
		)

	frappe.db.set_value(
		"MZ Tenant Provisioning", prov.name, {"status": "Archived", "backup_path": archived_path}
	)
	_log(prov, f"Arquivado. Backup em: {archived_path}")
	crm.report_for_contract(contract_name, crm.STAGE_CLOSED, status="Lost")
	if notify:
		send_lifecycle_email("archived", contract_name)


# ---------------------------------------------------------------------------
# The daily engine (F3)
# ---------------------------------------------------------------------------

def process_lifecycle():
	"""Daily: the two switch-off rules and the archive rule. Reads dates only,
	sees only contracts with a live site. Each rule executes only when its switch
	is on; otherwise it is reported in the digest."""
	settings = get_settings()
	actions = []

	def _report(what, armed):
		actions.append(what if armed else f"[só observação] {what}")

	# Rule 1 — trial ended without converting: start_date arrived, still unsigned.
	expired_trials = live_trials(
		fields=["name", "party_name", "start_date"],
		extra_conditions="AND c.start_date <= %(yesterday)s",
		values={"yesterday": add_days(nowdate(), -1)},
	)
	for c in expired_trials:
		_report(f"suspender (trial expirou em {c.start_date}): {c.name} — {c.party_name}", settings.auto_suspend)
		if settings.auto_suspend:
			_attempt(actions, c.name, lambda: suspend(
				c.name, reason=f"Trial terminou em {c.start_date} sem assinatura", cause="trial"
			))

	# Rule 2 — an invoice unpaid `overdue_days_to_suspend` past its due date.
	cutoff = add_days(nowdate(), -settings.overdue_days_to_suspend)
	overdue = frappe.db.sql(
		"""
		SELECT si.name, si.customer, si.subscription, si.outstanding_amount, si.due_date,
		       c.name AS contract
		FROM `tabSales Invoice` si
		JOIN `tabContract` c ON c.mz_linked_subscription = si.subscription
		JOIN `tabMZ Tenant Provisioning` p ON p.contract = c.name
		WHERE si.subscription IS NOT NULL
			AND si.outstanding_amount > 0
			AND si.docstatus = 1
			AND si.due_date <= %(cutoff)s
			AND c.is_signed = 1
			AND p.status = 'Active'
		""",
		{"cutoff": cutoff},
		as_dict=True,
	)
	for inv in overdue:
		_report(
			f"suspender (factura {inv.name} vencida em {inv.due_date}, {inv.outstanding_amount} em dívida): {inv.contract}",
			settings.auto_suspend,
		)
		if settings.auto_suspend:
			_attempt(actions, inv.contract, lambda: suspend(
				inv.contract,
				reason=f"Factura {inv.name} não paga {settings.overdue_days_to_suspend} dias após o vencimento",
				cause="overdue",
				invoice=inv.name,
			))

	# Rule 3 — suspended past the grace period: archive.
	grace_cutoff = add_to_date(now_datetime(), days=-settings.grace_days_to_archive)
	to_archive = frappe.get_all(
		"MZ Tenant Provisioning",
		filters={"status": "Suspended", "suspended_on": ("<=", grace_cutoff)},
		fields=["name", "contract", "site_name", "suspended_on"],
	)
	for prov in to_archive:
		if frappe.db.get_value("MZ Tenant Provisioning", prov.name, "status") != "Suspended":
			continue  # e.g. reactivated since the record was fetched
		_report(f"arquivar (suspenso desde {prov.suspended_on}): {prov.site_name} — {prov.contract}", settings.auto_archive)
		if settings.auto_archive:
			_attempt(actions, prov.contract, lambda: archive(prov.contract))

	_send_digest(settings, actions)
	frappe.db.commit()
	return actions


def _attempt(actions, contract_name, fn):
	"""One bad contract must never abort the run for the others: record, log, continue.
	Each action commits on success and rolls back on failure so the next starts clean."""
	try:
		fn()
		frappe.db.commit()
	except Exception as exc:
		frappe.db.rollback()
		actions.append(f"FALHOU {contract_name}: {str(exc)[:200]}")
		frappe.log_error(title=f"AI SaaS Lifecycle: {contract_name}", message=frappe.get_traceback())


def _send_digest(settings, actions):
	if not actions:
		return
	armed = [a for a in ("suspender" if settings.auto_suspend else "", "arquivar" if settings.auto_archive else "") if a]
	mode = "armado: " + ", ".join(armed) if armed else "SÓ OBSERVAÇÃO — nada foi executado"
	body = f"<p>Motor de ciclo de vida ({mode}), {nowdate()}:</p><ul>" + "".join(
		f"<li>{frappe.utils.escape_html(a)}</li>" for a in actions
	) + "</ul>"
	# The digest is operational, not an error: it goes to the ops inbox and the app log.
	frappe.logger("ai_saas").info("lifecycle [%s]: %s", mode, " | ".join(actions))
	notify_ops(f"Motor de ciclo de vida ({mode}): {len(actions)} acção(ões)", body)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def account_phase(contract_name) -> str:
	"""The account's phase, derived — never stored. Closed / Suspended follow the site,
	Active / Trial follow the signature; "" for a contract without a site."""
	prov_status = frappe.db.get_value("MZ Tenant Provisioning", {"contract": contract_name}, "status")
	if not prov_status:
		return ""
	if prov_status == "Archived":
		return "Closed"
	if prov_status == "Suspended":
		return "Suspended"
	return "Active" if frappe.db.get_value("Contract", contract_name, "is_signed") else "Trial"


def live_trials(fields=("name",), names=None, extra_conditions="", values=None):
	"""Submitted, unsigned contracts whose site is up — the trials the engine, the
	usage probe and the signup ceiling all mean. `names` restricts to those contracts."""
	values = dict(values or {})
	where = ""
	if names is not None:
		if not names:
			return []
		values["names"] = tuple(names)
		where = "AND c.name IN %(names)s"
	cols = ", ".join(f"c.`{f}`" for f in fields)
	return frappe.db.sql(
		f"""
		SELECT {cols}
		FROM `tabContract` c
		JOIN `tabMZ Tenant Provisioning` p ON p.contract = c.name
		WHERE c.docstatus = 1 AND c.is_signed = 0 AND IFNULL(c.mz_direct, 0) = 0 AND p.status = 'Active'
		{where} {extra_conditions}
		ORDER BY c.creation
		""",
		values,
		as_dict=True,
	)


def _has_overdue_invoice(contract_name) -> bool:
	sub = frappe.db.get_value("Contract", contract_name, "mz_linked_subscription")
	if not sub:
		return False
	return bool(frappe.db.exists(
		"Sales Invoice",
		{"subscription": sub, "docstatus": 1, "outstanding_amount": (">", 0),
		 "due_date": ("<=", add_days(nowdate(), -get_settings().overdue_days_to_suspend))},
	))


def _get_prov(contract_name):
	name = frappe.db.get_value("MZ Tenant Provisioning", {"contract": contract_name}, "name")
	if not name:
		frappe.throw(f"O contrato {contract_name} não tem registo de provisionamento.")
	return frappe.get_doc("MZ Tenant Provisioning", name)


def _set_maintenance_mode(prov, on: bool):
	bench = get_bench_cmd()
	_run(prov, [bench, "--site", prov.site_name, "set-maintenance-mode", "on" if on else "off"],
	     "set-maintenance-mode", MAINTENANCE_TIMEOUT)


def _run(prov, cmd, step, timeout):
	"""_run_cmd with the command output persisted even when the step then raises."""
	try:
		run_cmd(cmd, step, prov, timeout)
	finally:
		frappe.db.set_value("MZ Tenant Provisioning", prov.name, "log", prov.log, update_modified=False)


def _log(prov, message):
	"""Append to the record's log and persist — including whatever _run_cmd appended
	to the in-memory prov.log (command output) since the last persist."""
	prov.log = f"{prov.log or ''}\n[{now_datetime()}] {message}".strip()
	frappe.db.set_value("MZ Tenant Provisioning", prov.name, "log", prov.log, update_modified=False)


def _has_recent_backup(backups_dir, max_age_seconds=3600):
	"""A non-empty .sql.gz newer than an hour must exist before drop-site may run."""
	if not os.path.isdir(backups_dir):
		return False
	now = time.time()
	for f in os.listdir(backups_dir):
		if f.endswith(".sql.gz"):
			path = os.path.join(backups_dir, f)
			if os.path.getsize(path) > 0 and now - os.path.getmtime(path) < max_age_seconds:
				return True
	return False

