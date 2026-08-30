"""Usage probe and commercial signals (docs/sales-funnel-implementation.md, D1-D2).

Daily, for every contract in phase Trial with a live site: run the read-only
probe inside the tenant (`bench --site <site> execute erpnext_mz.utils.tenant_usage`),
store the numbers as an MZ Tenant Usage Snapshot on the control site, and
score the account (momentum + breadth) and derive three signals: Engaged (the
score crosses the threshold), Cooling (was active, went silent — decay) and Cold
(past the trial's midpoint with no activity at all). A dead site never stalls the sweep:
per-site timeout, the failure recorded on the snapshot row, next site.

Runs on the scheduler's default queue (daily), never on `long` — with a single
worker, a 20-minute `bench new-site` on `long` would otherwise block it.
"""

import json

import frappe
from frappe.utils import add_days, date_diff, getdate, nowdate

from ai_saas.saas import crm
from ai_saas.saas.provisioning import ProvisioningError, get_bench_cmd, run_cmd_capture

PROBE_TIMEOUT = 60
STAGE_ENGAGED = crm.STAGE_TRIAL_ENGAGED
STAGE_AT_RISK = crm.STAGE_TRIAL_AT_RISK
MARKER_HOT = "[Lead quente]"
MARKER_COLD = "[Lead frio]"
MARKER_COOLING = "[Lead a arrefecer]"

# Scoring: every rule is a count the probe returns, so a salesperson can read the
# ToDo and see exactly why. Engaged needs momentum AND at least one breadth point —
# one invoice created with our help on the activation call scores 0.
ENGAGED_THRESHOLD = 3
SILENT_DAYS = 7  # no login for this long, after having been active → Cooling

INT_FIELDS = (
	"invoice_count", "user_count", "invoice_days_7d", "invoice_days_prev_7d",
	"invoice_count_30d", "active_users_7d", "master_data_count", "other_docs_30d",
)

# The probe lives in erpnext_mz because that app is installed on every tenant site;
# `bench execute` refuses a method from an app the site does not have.
PROBE_METHOD = "erpnext_mz.utils.tenant_usage.usage_snapshot"


def collect_usage_snapshots(contracts=None):
	"""Daily job: probe every trial site, store a snapshot, evaluate the signals.

	`contracts` restricts the sweep to those contract names — for a manual re-run on
	one account, and for tests, which must never touch the real trials on the site."""
	today = getdate(nowdate())
	from ai_saas.saas.tenant_lifecycle import live_trials

	trials = live_trials(fields=["name", "party_name", "creation", "start_date"], names=contracts)
	for contract in trials:
		prov = frappe.db.get_value(
			"MZ Tenant Provisioning",
			{"contract": contract.name, "status": "Active"},
			["name", "site_name"],
			as_dict=True,
		)
		if not prov:
			continue  # not provisioned yet, or suspended — nothing to read
		if frappe.db.exists(
			"MZ Tenant Usage Snapshot", {"contract": contract.name, "snapshot_date": today}
		):
			continue

		try:
			snapshot = _probe(prov.site_name)
			row = frappe.get_doc({
				"doctype": "MZ Tenant Usage Snapshot",
				"contract": contract.name,
				"site_name": prov.site_name,
				"snapshot_date": today,
				**snapshot,
			})
			row.insert(ignore_permissions=True)
			if row.probe_ok:
				evaluate_signals(contract, row)
			frappe.db.commit()
		except Exception:
			# One trial's bad data (a closed Opportunity, a disabled assignee) must not
			# stop the sweep for the others.
			frappe.db.rollback()
			frappe.log_error(title=f"AI SaaS usage sweep: {contract.name}", message=frappe.get_traceback())

	if contracts is None:
		# The report belongs to the full daily sweep, not to a one-account re-run.
		send_daily_usage_report()


SIGNAL_LABELS = {"Engaged": "🔥 Quente", "Cooling": "🌡 A arrefecer", "Cold": "❄ Frio", "": "—"}


def usage_report_rows(date=None) -> list:
	"""Today's snapshot per trial contract, with the commercial context the team reads."""
	date = date or nowdate()
	rows = frappe.db.sql(
		"""select s.contract, s.site_name, s.probe_ok, s.error, s.invoice_count, s.invoice_days_7d,
		          s.active_users_7d, s.master_data_count, s.other_docs_30d, s.last_login,
		          s.engagement_score, s.`signal`, c.party_name, c.start_date
		   from `tabMZ Tenant Usage Snapshot` s
		   join `tabContract` c on c.name = s.contract
		   join `tabMZ Tenant Provisioning` p on p.contract = c.name
		   where s.snapshot_date = %s and c.docstatus = 1 and c.is_signed = 0 and p.status = 'Active'
		   order by s.engagement_score desc, c.start_date asc""",
		(date,),
		as_dict=True,
	)
	for r in rows:
		r.days_left = date_diff(r.start_date, date)
		opp = crm.find_opportunity(r.contract)
		r.opportunity = opp
		r.assignee = ""
		if opp:
			raw_assign = frappe.db.get_value("Opportunity", opp, "_assign")
			try:
				r.assignee = (json.loads(raw_assign or "[]") or [""])[0]
			except ValueError:
				r.assignee = ""
	return rows


def send_daily_usage_report(date=None) -> bool:
	"""One email to the sales team after the sweep: every trial, its numbers, its signal.
	Nothing is sent on a day with no trials. Returns whether an email was queued."""
	from ai_saas.saas.tenant_lifecycle import get_settings

	date = date or nowdate()
	rows = usage_report_rows(date)
	if not rows:
		return False
	settings = get_settings()
	recipients = settings.usage_report_recipients
	if not recipients and settings.default_sales_user:
		recipients = [frappe.db.get_value("User", settings.default_sales_user, "email")]
	if not recipients:
		from ai_saas.saas.alerts import ops_alert_recipients

		recipients = ops_alert_recipients()
	recipients = [e for e in recipients if e]
	if not recipients:
		return False

	counts = {k: sum(1 for r in rows if r.signal == k) for k in ("Engaged", "Cooling", "Cold")}
	subject = (
		f"Trials {frappe.utils.formatdate(date)}: {len(rows)} activos · {counts['Engaged']} quentes · "
		f"{counts['Cooling']} a arrefecer · {counts['Cold']} frios"
	)
	frappe.sendmail(
		recipients=recipients,
		subject=subject,
		message=frappe.render_template(
			"ai_saas/templates/emails/daily_usage_report.html",
			{"rows": rows, "date": date, "counts": counts, "labels": SIGNAL_LABELS},
		),
		delayed=False,
	)
	return True


def _probe(site_name) -> dict:
	"""Run the probe; never raise — a dead site is recorded, not fatal."""
	scratch = frappe._dict(log="")  # _run_cmd_capture appends its log to this, not to a record
	try:
		raw = run_cmd_capture(
			[get_bench_cmd(), "--site", site_name, "execute", PROBE_METHOD],
			step="usage-probe",
			prov=scratch,
			timeout=PROBE_TIMEOUT,
		)
		# bench execute JSON-encodes the return value (a JSON string) on stdout.
		data = json.loads(raw.strip())
		if isinstance(data, str):
			data = json.loads(data)
		snapshot = {field: int(data.get(field) or 0) for field in INT_FIELDS}
		snapshot.update(
			first_invoice_date=data.get("first_invoice_date") or None,
			last_login=data.get("last_login") or None,
			probe_ok=1,
			error="",
		)
		return snapshot
	except (ProvisioningError, ValueError, TypeError) as exc:
		return {"probe_ok": 0, "error": str(exc)[:1000]}


def score(snapshot) -> tuple[int, list[str]]:
	"""Engagement score and the reasons behind it, in the salesperson's words."""
	points, reasons = 0, []
	days = int(snapshot.get("invoice_days_7d") or 0)
	prev = int(snapshot.get("invoice_days_prev_7d") or 0)
	if days >= 2:
		points += 2
		reasons.append(f"facturou em {days} dias distintos esta semana")
	if days > prev and prev > 0:
		points += 1
		reasons.append(f"a acelerar ({prev} → {days} dias com facturas)")
	if int(snapshot.get("active_users_7d") or 0) >= 2:
		points += 1
		reasons.append(f"{snapshot.active_users_7d} utilizadores activos esta semana")
	if int(snapshot.get("master_data_count") or 0) >= 5:
		points += 1
		reasons.append(f"{snapshot.master_data_count} clientes/artigos/fornecedores próprios")
	if int(snapshot.get("other_docs_30d") or 0) >= 1:
		points += 1
		reasons.append(f"{snapshot.other_docs_30d} documentos além de facturas (pagamentos, compras, stock, salários)")
	return points, reasons


def evaluate_signals(contract, snapshot):
	"""D2: Engaged / Cooling / Cold. Stage changes fire once (dedup on stage);
	each ToDo marker is open at most once per Opportunity."""
	points, reasons = score(snapshot)
	signal = ""
	if points >= ENGAGED_THRESHOLD:
		signal = "Engaged"
	elif _went_silent(contract, snapshot):
		signal = "Cooling"
	elif _past_midpoint(contract) and _nothing_done(snapshot):
		signal = "Cold"
	if snapshot.get("name"):
		frappe.db.set_value(
			"MZ Tenant Usage Snapshot", snapshot.name,
			{"engagement_score": points, "signal": signal}, update_modified=False,
		)

	opportunity = crm.find_opportunity(contract.name)
	if not opportunity or not signal:
		return signal  # legacy contract without an Opportunity, or nothing to say today

	if signal == "Engaged":
		if crm.report(opportunity, STAGE_ENGAGED):
			crm.create_sales_todo(
				opportunity,
				f"{contract.party_name} está a usar o sistema a sério ({points} pontos): "
				+ "; ".join(reasons) + ". Contactar para activação.",
				MARKER_HOT,
			)
	elif signal == "Cooling":
		if crm.report(opportunity, STAGE_AT_RISK):
			crm.create_sales_todo(
				opportunity,
				f"{contract.party_name} esteve activo e parou: sem login há {SILENT_DAYS}+ dias "
				f"e sem facturas esta semana (trial termina em {contract.start_date}). "
				"Contactar antes que expire.",
				MARKER_COOLING,
			)
	elif signal == "Cold":
		if crm.report(opportunity, STAGE_AT_RISK):
			crm.create_sales_todo(
				opportunity,
				f"{contract.party_name} está a meio do trial (termina em {contract.start_date}) "
				f"sem nenhum login nem factura. Contactar antes que expire.",
				MARKER_COLD,
			)
	return signal


def _nothing_done(snapshot) -> bool:
	"""No invoice, no own master data: logging in once to look around is not activity."""
	return not snapshot.get("invoice_count") and not snapshot.get("master_data_count")


def _went_silent(contract, snapshot) -> bool:
	"""Decay: the account did real work before (an earlier snapshot with an invoice or
	a score) and now shows no login for SILENT_DAYS and no invoice this week."""
	if snapshot.get("invoice_days_7d") or not snapshot.get("last_login"):
		# Still invoicing → not silent. Never logged in → Cold territory, not decay.
		return False
	silent_since = add_days(nowdate(), -SILENT_DAYS)
	if getdate(snapshot.last_login) > getdate(silent_since):
		return False
	earlier = {"contract": contract.name, "probe_ok": 1, "snapshot_date": ("<", nowdate())}
	return bool(
		frappe.db.exists("MZ Tenant Usage Snapshot", {**earlier, "invoice_count": (">", 0)})
		or frappe.db.exists("MZ Tenant Usage Snapshot", {**earlier, "engagement_score": (">", 0)})
	)


def _past_midpoint(contract) -> bool:
	"""Halfway between the contract's creation and its start_date (trial end) — dates, not day counts."""
	created = getdate(contract.creation)
	end = getdate(contract.start_date)
	total = date_diff(end, created)
	if total <= 0:
		return True
	return date_diff(getdate(nowdate()), created) * 2 >= total

