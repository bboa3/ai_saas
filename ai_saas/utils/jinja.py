"""Helpers exposed to Jinja (hooks.jinja) — usable in Email Templates and Notifications.

Every top-level function here becomes a Jinja global, so keep the module to what the
templates need. The communication language (docs/communication-copy-review.md):
relationship emails greet the person by the time of day and sign with the account
manager's name; formal (billing) emails do neither.
"""

import re

import frappe

from ai_saas.saas.activation import get_activation_url, get_reactivation_url

__all__ = ["get_activation_url", "get_reactivation_url", "mz_first_name", "mz_greeting", "mz_signature"]

TEAM = "Equipa MozEconomia Cloud"


def mz_first_name(full_name=None) -> str:
	"""First token of a person's name — '' when nothing usable is given. Accepts a Contact
	ID too ("Ana Silva-Mais Forte, LDA"): the Contract mirrors the Customer's primary
	contact by its ID, and the greeting must still read 'Ana'."""
	if not full_name:
		return ""
	return re.split(r"[ \-]", full_name.strip(), maxsplit=1)[0]


def mz_greeting(full_name=None) -> str:
	"""'Bom dia Ana,' / 'Boa tarde Ana,' / 'Boa noite Ana,' from the site-local sending time
	(Africa/Maputo). Bare 'Bom dia,' when the name is unknown. Scheduled notifications are
	pinned to 08:00 (install.ensure_daily_alerts_hour) so they always read 'Bom dia'."""
	hour = frappe.utils.now_datetime().hour
	salutation = "Bom dia" if hour < 12 else ("Boa tarde" if hour < 19 else "Boa noite")
	first = mz_first_name(full_name)
	return f"{salutation} {first}," if first else f"{salutation},"


def mz_signature(user=None) -> str:
	"""Relationship sign-off: 'Com boas energias,' + account manager + team. `user` is the
	Contract's mz_account_manager; falls back to MZ SaaS Settings.default_sales_user, then
	to the team alone."""
	user = user or frappe.db.get_single_value("MZ SaaS Settings", "default_sales_user")
	full_name = frappe.db.get_value("User", user, "full_name") if user else ""
	person = f"<strong>{frappe.utils.escape_html(full_name)}</strong><br>" if full_name else ""
	return f"<p>Com boas energias,<br>{person}{TEAM}</p>"
