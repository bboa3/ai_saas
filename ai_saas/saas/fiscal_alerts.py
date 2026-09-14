"""Alerta Fiscal — content layer 1 (docs/content-layers-implementation.md).

A fixed text per obligation, sent N business days before each deadline to every Lead
who subscribed (Lead.mz_fiscal_alerts) and never asked to leave (Lead.unsubscribed).
Not stage-gated: a customer on trial or paying still wants the deadline. The table
is `MZ Fiscal Obligation`; the business-day rule is utils/dates.py.

Skip-tolerant by design: the existing Days After sequences fire only on the exact
day and a missed scheduler run loses the touch. Here the job fires anywhere in the
window [alert_date, deadline] and stamps `last_alerted_deadline`, so it sends once
per deadline whichever day it first runs.
"""

import calendar
from datetime import date

import frappe
from frappe.utils import cint, formatdate, getdate, nowdate

from ai_saas.saas.alerts import notify_ops
from ai_saas.saas.settings import get_settings
from ai_saas.utils.dates import holiday_dates, holiday_list_covers, subtract_business_days

FOOTER = (
	'<p style="font-size:12px;color:#5a6270;margin-top:24px">Alerta Fiscal da MozEconomia Cloud — '
	"software de facturação certificado pela Autoridade Tributária de Moçambique. "
	'<a href="{unsubscribe_url}" style="color:#5a6270">Deixar de receber</a>.</p>'
)


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------


def _clamp(year, month, day) -> date:
	return date(year, month, min(cint(day), calendar.monthrange(year, month)[1]))


def next_deadline(obligation, today=None) -> date:
	"""The first deadline on or after `today` for this obligation's rule."""
	today = getdate(today or nowdate())
	day = cint(obligation.deadline_day)
	if obligation.periodicity == "Annual":
		d = _clamp(today.year, cint(obligation.deadline_month), day)
		return d if d >= today else _clamp(today.year + 1, cint(obligation.deadline_month), day)
	if obligation.periodicity == "Quarterly":
		first = cint(obligation.deadline_month) or 1
		months = [m for m in range(1, 13) if (m - first) % 3 == 0]
		for year in (today.year, today.year + 1):
			for m in months:
				d = _clamp(year, m, day)
				if d >= today:
					return d
	# Monthly
	d = _clamp(today.year, today.month, day)
	if d >= today:
		return d
	year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
	return _clamp(year, month, day)


# ---------------------------------------------------------------------------
# recipients
# ---------------------------------------------------------------------------


def segments_with_code(code: str) -> list[str]:
	"""Segments whose Segment Intelligence Map lists this code under 'Conformidade MZ'."""
	return frappe.get_all(
		"Conformidade MZ",
		{"parenttype": "Segment Intelligence Map", "conformidade": code},
		pluck="parent",
		distinct=True,
	)


def eligible_leads(obligation) -> list:
	filters = {"mz_fiscal_alerts": 1, "unsubscribed": 0, "email_id": ("!=", "")}
	if not obligation.applies_to_all_segments:
		segments = segments_with_code(obligation.code)
		if not segments:
			return []
		filters["mz_segment"] = ("in", segments)
	if obligation.requires_vat:
		filters["mz_tax_regime"] = ("not in", ["Isento"])
	return frappe.get_all(
		"Lead",
		filters=filters,
		fields=["name", "email_id", "first_name", "lead_name", "company_name", "mz_segment", "mz_tax_regime"],
		order_by="name",
	)


# ---------------------------------------------------------------------------
# sending
# ---------------------------------------------------------------------------


def render_alert(obligation, lead, deadline) -> dict:
	from ai_saas.saas.content import unsubscribe_url

	context = {
		"deadline": formatdate(deadline, "dd/MM/yyyy"),
		"deadline_date": deadline,
		"lead": lead,
		"obligation": obligation,
	}
	subject = frappe.render_template(obligation.alert_subject or obligation.title, context)
	body = frappe.render_template(obligation.alert_body or "", context)
	message = (
		'<div style="font-family:Arial,Helvetica,sans-serif;max-width:600px;margin:0 auto;color:#020202">'
		+ body
		+ FOOTER.format(unsubscribe_url=unsubscribe_url(lead.name))
		+ "</div>"
	)
	return {"subject": subject, "message": message}


def send_alert(obligation, lead, deadline) -> None:
	mail = render_alert(obligation, lead, deadline)
	frappe.sendmail(
		recipients=[lead.email_id],
		subject=mail["subject"],
		message=mail["message"],
		reference_doctype="Lead",
		reference_name=lead.name,
	)


def send_due_alerts(today=None, dry_run=False) -> dict:
	"""Daily job (hooks.scheduler_events). Returns what it did, per obligation."""
	today = getdate(today or nowdate())
	settings = get_settings()
	lead_days = settings.fiscal_alert_lead_days
	holidays = holiday_dates(settings.holiday_list)
	_warn_if_uncovered(settings.holiday_list, today)

	summary = {}
	rows = frappe.get_all(
		"MZ Fiscal Obligation", {"enabled": 1, "reviewed_on": ("is", "set")}, pluck="name", order_by="name"
	)
	for name in rows:
		ob = frappe.get_doc("MZ Fiscal Obligation", name)
		deadline = next_deadline(ob, today)
		alert_date = subtract_business_days(deadline, lead_days, holidays=holidays)
		if not (alert_date <= today <= deadline):
			summary[name] = {
				"deadline": deadline,
				"alert_date": alert_date,
				"sent": 0,
				"reason": "not in window",
			}
			continue
		if ob.last_alerted_deadline and getdate(ob.last_alerted_deadline) == deadline:
			summary[name] = {
				"deadline": deadline,
				"alert_date": alert_date,
				"sent": 0,
				"reason": "already sent",
			}
			continue
		leads = eligible_leads(ob)
		if not dry_run:
			for lead in leads:
				try:
					send_alert(ob, lead, deadline)
				except Exception:
					frappe.log_error(
						title=f"AI SaaS: fiscal alert {name} to {lead.name} not sent",
						message=frappe.get_traceback(),
					)
			ob.db_set("last_alerted_deadline", deadline, update_modified=False)
		summary[name] = {
			"deadline": deadline,
			"alert_date": alert_date,
			"sent": len(leads),
			"reason": "dry run" if dry_run else "",
		}
	return summary


def _warn_if_uncovered(holiday_list, today) -> None:
	"""One ops alert per year when the Holiday List does not reach the current date —
	alerts still go out (weekends only), but someone must extend the list."""
	if holiday_list_covers(holiday_list, today):
		return
	key = f"ai_saas:holiday_list_warned:{today.year}"
	if frappe.cache().get_value(key):
		return
	frappe.cache().set_value(key, 1, expires_in_sec=60 * 60 * 24 * 30)
	notify_ops(
		f"Lista de feriados não cobre {today.year}",
		f"<p>A lista <strong>{frappe.utils.escape_html(holiday_list or '')}</strong> (MZ SaaS Settings) não cobre "
		f"{formatdate(today, 'dd/MM/yyyy')}. O Alerta Fiscal está a contar dias úteis só com fins-de-semana até a lista ser prolongada.</p>",
	)
