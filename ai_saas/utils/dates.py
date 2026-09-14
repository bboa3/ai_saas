"""Business-day arithmetic over an ERPNext Holiday List.

Frappe has add_days and nothing else; ERPNext's is_holiday() answers one date. The
fiscal alert (saas/fiscal_alerts.py) needs "N business days before a deadline", so
that is all this module does. Weekends are always skipped — a Holiday List that was
generated with a weekly off already contains them, one without must not silently
turn Saturday into a business day.
"""

from datetime import date

import frappe
from frappe.utils import add_days, getdate


def holiday_dates(holiday_list: str | None) -> set[date]:
	"""Every date in the list, loaded once. Empty set for a missing list."""
	if not holiday_list or not frappe.db.exists("Holiday List", holiday_list):
		return set()
	return {getdate(d) for d in frappe.get_all("Holiday", {"parent": holiday_list}, pluck="holiday_date")}


def holiday_list_covers(holiday_list: str | None, day) -> bool:
	"""True when the list's from/to range contains `day` — the check the alert job
	makes once a year, so an expired list is noticed before it matters."""
	if not holiday_list:
		return False
	span = frappe.db.get_value("Holiday List", holiday_list, ["from_date", "to_date"], as_dict=True)
	if not span:
		return False
	return getdate(span.from_date) <= getdate(day) <= getdate(span.to_date)


def is_business_day(day, holidays: set[date]) -> bool:
	day = getdate(day)
	return day.weekday() < 5 and day not in holidays


def subtract_business_days(day, n: int, holiday_list: str | None = None, holidays: set[date] | None = None):
	"""The date `n` business days before `day` (n=0 returns `day` itself). Pass
	`holidays` to reuse a loaded set across many calls."""
	if holidays is None:
		holidays = holiday_dates(holiday_list)
	d = getdate(day)
	remaining = int(n or 0)
	while remaining > 0:
		d = add_days(d, -1)
		if is_business_day(d, holidays):
			remaining -= 1
	return d
