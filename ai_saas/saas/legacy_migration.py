"""Inventory of every tenant site, crossed with what the control site knows.

Phase 1 of the legacy-account migration (decision 2026-08-30): before anything is
registered, archived or campaigned, *look*. The sites are the ground truth — each one
says who it belongs to (Company, its System Managers) and whether it is used — and
the control site's Customers, Contracts, Subscriptions, Opportunities and Leads are
matched **to** them through every key available (site name, NUIT, emails, mobiles,
company name), each match labelled with the key that produced it.

	bench --site <control-site> execute ai_saas.saas.legacy_migration.inventory

writes `tenant_inventory_<YYYYMMDD>.xlsx` (sheets: sites, control_only, summary) as a
private File attached to MZ SaaS Settings — downloadable from the desk — and prints
the URL and the counts. Read-only apart from that File record; idempotent (the same
day's file is replaced).
"""

import datetime
import io
import json
import os
import re
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher

import frappe
from frappe.utils import add_days, cint, flt, getdate, nowdate

from ai_saas.saas.provisioning import (
	DOMAINS,
	ProvisioningError,
	get_bench_cmd,
	get_bench_path,
	run_cmd_capture,
)

IDENTITY_METHOD = "erpnext_mz.utils.tenant_usage.identity"
PROBE_TIMEOUT = 60
HTTP_TIMEOUT = 5
PAID_RECENTLY_DAYS = 60
FUZZY_THRESHOLD = 0.85
STRONG_KEYS = ("site", "nuit", "email")
SITE_COLUMNS = (
	"site", "site_dir", "maintenance_mode", "http_status",
	"company_name", "nuit", "company_email", "company_phone", "responsible", "users",
	"last_login", "invoice_count", "last_invoice_on", "invoice_count_30d", "master_data_count",
	"last_doc_modified", "db_size_mb", "last_backup_on", "probe_error",
	"customer", "contract", "is_signed", "plan", "sub_status", "sub_cancelled_on",
	"invoices", "paid_invoices", "outstanding", "last_billed_on", "last_paid_on", "opportunity", "sales_stage", "lead", "prov_status",
	"match_keys", "match_quality", "conflicts", "class", "class_reason",
)
CONTROL_COLUMNS = ("record", "name", "company_name", "nuit", "email", "mobile", "contract", "is_signed",
                   "mz_tenant", "sub_status", "opportunity", "sales_stage", "opp_status", "lead")


# ---------------------------------------------------------------- sites on disk

def _sites() -> list[dict]:
	"""Every tenant site directory, live or archived, with what the filesystem knows."""
	bench = get_bench_path()
	found = []
	for state, root in (("live", os.path.join(bench, "sites")), ("archived", os.path.join(bench, "archived", "sites"))):
		if not os.path.isdir(root):
			continue
		for name in sorted(os.listdir(root)):
			path = os.path.join(root, name)
			if not os.path.isdir(path) or not _is_tenant_site(name):
				continue
			found.append({
				"site": name, "site_dir": state, "path": path,
				"maintenance_mode": _maintenance_mode(path),
				"last_backup_on": _last_backup(path),
			})
	return found


def _is_tenant_site(name: str) -> bool:
	return any(name.endswith(d) for d in DOMAINS)


def _maintenance_mode(path) -> int:
	try:
		with open(os.path.join(path, "site_config.json")) as f:
			return cint(json.load(f).get("maintenance_mode"))
	except (OSError, ValueError):
		return 0


def _last_backup(path):
	folder = os.path.join(path, "private", "backups")
	try:
		files = [os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".sql.gz")]
	except OSError:
		return None
	if not files:
		return None
	newest = max(files, key=os.path.getmtime)
	return datetime.datetime.fromtimestamp(os.path.getmtime(newest)).replace(microsecond=0)


def _http_status(site: str) -> str:
	"""What the internet sees: 200 / 503 / dns-fail / timeout / conn-fail."""
	import requests

	try:
		r = requests.get(f"https://{site}/api/method/ping", timeout=HTTP_TIMEOUT, allow_redirects=True)
		return str(r.status_code)
	except requests.exceptions.ConnectTimeout:
		return "timeout"
	except requests.exceptions.ConnectionError as exc:
		return "dns-fail" if "Name or service not known" in str(exc) or "nodename" in str(exc) else "conn-fail"
	except requests.exceptions.RequestException as exc:  # pragma: no cover
		return type(exc).__name__


def _probe_identity(site: str) -> dict:
	"""Run erpnext_mz's identity() inside the tenant; never raise."""
	scratch = frappe._dict(log="")
	try:
		raw = run_cmd_capture(
			[get_bench_cmd(), "--site", site, "execute", IDENTITY_METHOD],
			step="identity-probe", prov=scratch, timeout=PROBE_TIMEOUT,
		)
		data = json.loads(raw.strip())
		if isinstance(data, str):
			data = json.loads(data)
		data["error"] = ""
		return data
	except (ProvisioningError, ValueError, TypeError) as exc:
		return {"error": str(exc)[:500], "company": {}, "people": [], "usage": {}}


# ---------------------------------------------------------------- matching

def _norm_name(value) -> str:
	if not value:
		return ""
	s = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode().lower()
	s = re.sub(r"\b(lda|limitada|l\.da|sa|s\.a|su|e\.i|ei|unipessoal|sociedade|company|empresa)\b", " ", s)
	return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _norm_mobile(value) -> str:
	digits = re.sub(r"\D", "", str(value or ""))
	return digits[-9:] if len(digits) >= 9 else ""


def _norm_nuit(value) -> str:
	return re.sub(r"\D", "", str(value or ""))


def _norm_email(value) -> str:
	return (value or "").strip().lower()


def _keys_from(site: str, ident: dict) -> dict:
	"""What the site offers to be matched on."""
	company = ident.get("company") or {}
	people = ident.get("people") or []
	return {
		"site": site,
		"slug": site.split(".")[0],
		"nuit": _norm_nuit(company.get("tax_id")),
		"emails": {e for e in (_norm_email(company.get("email")), *(_norm_email(p.get("email")) for p in people)) if e and "@" in e},
		"mobiles": {m for m in (_norm_mobile(company.get("phone_no")), *(_norm_mobile(p.get("mobile_no")) for p in people)) if m},
		"name": _norm_name(company.get("company_name")),
	}


def _match(site: str, ident: dict) -> dict:
	"""Control-side records that point at this site, each with the keys that hit.

	Returns {"customers": {name: {keys}}, "contracts": {...}, "leads": {...},
	"opportunities": {...}, "signups": {...}} — every candidate, never a guess.
	"""
	k = _keys_from(site, ident)
	hits = {t: defaultdict(set) for t in ("customers", "contracts", "leads", "opportunities", "signups")}

	def hit(kind, name, key):
		if name:
			hits[kind][name].add(key)

	# site name
	for c in frappe.get_all("Contract", filters={"docstatus": ("<", 2), "mz_tenant": k["slug"]}, fields=["name", "party_name", "party_type"]):
		hit("contracts", c.name, "site")
		if c.party_type == "Customer":
			hit("customers", c.party_name, "site")
	for c in frappe.get_all("Contract", filters={"docstatus": ("<", 2), "mz_tenant_url": site}, fields=["name", "party_name", "party_type"]):
		hit("contracts", c.name, "site")
		if c.party_type == "Customer":
			hit("customers", c.party_name, "site")
	for p in frappe.get_all("MZ Tenant Provisioning", filters={"site_name": site}, fields=["contract"]):
		hit("contracts", p.contract, "site")
		party = frappe.db.get_value("Contract", p.contract, ["party_type", "party_name"], as_dict=True)
		if party and party.party_type == "Customer":
			hit("customers", party.party_name, "site")
	for s in frappe.get_all("MZ Signup", filters={"subdomain": k["slug"]}, fields=["name", "customer", "lead", "contract", "opportunity"]):
		hit("signups", s.name, "site"); hit("customers", s.customer, "site"); hit("leads", s.lead, "site")
		hit("contracts", s.contract, "site"); hit("opportunities", s.opportunity, "site")

	# NUIT
	if k["nuit"]:
		for c in frappe.get_all("Customer", filters={"tax_id": ("like", f"%{k['nuit']}%")}, pluck="name"):
			if _norm_nuit(frappe.db.get_value("Customer", c, "tax_id")) == k["nuit"]:
				hit("customers", c, "nuit")
		for s in frappe.get_all("MZ Signup", filters={"tax_id": ("like", f"%{k['nuit']}%")}, fields=["name", "customer", "lead", "contract", "opportunity"]):
			hit("signups", s.name, "nuit"); hit("customers", s.customer, "nuit"); hit("leads", s.lead, "nuit")
			hit("contracts", s.contract, "nuit"); hit("opportunities", s.opportunity, "nuit")

	# emails
	for email in k["emails"]:
		for c in frappe.get_all("Customer", filters={"email_id": email}, pluck="name"):
			hit("customers", c, "email")
		contacts = frappe.get_all("Contact Email", filters={"email_id": email}, pluck="parent")
		if contacts:
			for link in frappe.get_all("Dynamic Link", filters={"parenttype": "Contact", "parent": ("in", contacts), "link_doctype": "Customer"}, pluck="link_name"):
				hit("customers", link, "email")
		for lead in frappe.get_all("Lead", filters={"email_id": email}, pluck="name"):
			hit("leads", lead, "email")
		for o in frappe.get_all("Opportunity", filters={"contact_email": email}, pluck="name"):
			hit("opportunities", o, "email")
		for s in frappe.get_all("MZ Signup", filters={"email": email}, fields=["name", "customer", "lead", "contract", "opportunity"]):
			hit("signups", s.name, "email"); hit("customers", s.customer, "email"); hit("leads", s.lead, "email")
			hit("contracts", s.contract, "email"); hit("opportunities", s.opportunity, "email")
		for c in frappe.get_all("Contract", filters={"docstatus": ("<", 2), "contact_email": email}, fields=["name", "party_name", "party_type"]):
			hit("contracts", c.name, "email")
			if c.party_type == "Customer":
				hit("customers", c.party_name, "email")

	# mobiles (last 9 digits)
	if k["mobiles"]:
		for c in frappe.get_all("Customer", filters={"mobile_no": ("is", "set")}, fields=["name", "mobile_no"]):
			if _norm_mobile(c.mobile_no) in k["mobiles"]:
				hit("customers", c.name, "mobile")
		for p in frappe.get_all("Contact Phone", filters={"phone": ("is", "set")}, fields=["parent", "phone"]):
			if _norm_mobile(p.phone) in k["mobiles"]:
				for link in frappe.get_all("Dynamic Link", filters={"parenttype": "Contact", "parent": p.parent, "link_doctype": "Customer"}, pluck="link_name"):
					hit("customers", link, "mobile")
		for lead in frappe.get_all("Lead", filters={"mobile_no": ("is", "set")}, fields=["name", "mobile_no", "phone"]):
			if _norm_mobile(lead.mobile_no) in k["mobiles"] or _norm_mobile(lead.phone) in k["mobiles"]:
				hit("leads", lead.name, "mobile")

	# company name — exact after normalisation, else fuzzy
	if k["name"]:
		for kind, doctype, field in (("customers", "Customer", "customer_name"), ("leads", "Lead", "company_name"), ("opportunities", "Opportunity", "customer_name")):
			for r in frappe.get_all(doctype, filters={field: ("is", "set")}, fields=["name", field]):
				n = _norm_name(r.get(field))
				if not n:
					continue
				if n == k["name"]:
					hit(kind, r.name, "name")
				elif SequenceMatcher(None, n, k["name"]).ratio() >= FUZZY_THRESHOLD:
					hit(kind, r.name, "fuzzy")

	return {t: {n: sorted(keys) for n, keys in d.items()} for t, d in hits.items()}


def _pick(candidates: dict) -> tuple[str | None, str, str]:
	"""The best candidate, its quality, and a note when the choice was not clean."""
	if not candidates:
		return None, "none", ""

	def strength(item):
		keys = item[1]
		return (sum(k in STRONG_KEYS for k in keys), "name" in keys, "mobile" in keys, len(keys))

	ranked = sorted(candidates.items(), key=strength, reverse=True)
	best, keys = ranked[0]
	quality = "strong" if any(k in STRONG_KEYS for k in keys) else ("name-only" if "name" in keys else ("mobile-only" if "mobile" in keys else "fuzzy"))
	conflicts = ""
	strong_others = [n for n, ks in ranked[1:] if any(k in STRONG_KEYS for k in ks)]
	if strong_others:
		conflicts = "also " + ", ".join(f"{n} ({';'.join(candidates[n])})" for n in strong_others)
	return best, quality, conflicts


# ---------------------------------------------------------------- control side

def _control_side(customer: str | None, contracts_hit: dict, opps_hit: dict, leads_hit: dict) -> dict:
	"""What the control site records about this account."""
	out = {}
	contract = None
	if customer:
		contract = frappe.db.get_value(
			"Contract", {"party_type": "Customer", "party_name": customer, "docstatus": ("<", 2)},
			["name", "is_signed", "mz_subscription_plan", "mz_linked_subscription", "mz_tenant", "docstatus"],
			as_dict=True, order_by="docstatus desc, creation desc",
		)
	if not customer and contracts_hit:
		# No Customer matched, but a Contract did (by site or email): take the account from it.
		contract = frappe.db.get_value("Contract", sorted(contracts_hit)[-1], ["name", "is_signed", "mz_subscription_plan", "mz_linked_subscription", "mz_tenant", "docstatus"], as_dict=True)
		party = frappe.db.get_value("Contract", contract.name, ["party_type", "party_name"], as_dict=True) if contract else None
		customer = party.party_name if party and party.party_type == "Customer" else None
	out["customer"] = customer
	out["contract"] = contract.name if contract else None
	out["is_signed"] = cint(contract.is_signed) if contract else None
	out["plan"] = contract.mz_subscription_plan if contract else None
	out["prov_status"] = frappe.db.get_value("MZ Tenant Provisioning", {"contract": contract.name}, "status") if contract else None

	sub = None
	if contract and contract.mz_linked_subscription:
		sub = frappe.db.get_value("Subscription", contract.mz_linked_subscription, ["name", "status", "cancelation_date"], as_dict=True)
	if not sub and customer:
		sub = frappe.db.get_value("Subscription", {"party_type": "Customer", "party": customer}, ["name", "status", "cancelation_date"], as_dict=True, order_by="creation desc")
	out["sub_status"] = sub.status if sub else None
	out["sub_cancelled_on"] = sub.cancelation_date if sub else None

	if customer:
		inv = frappe.db.sql(
			"""select count(*) n, sum(outstanding_amount = 0) paid, sum(outstanding_amount) outstanding, max(posting_date) last
			   from `tabSales Invoice` where customer = %s and docstatus = 1 and is_return = 0""",
			customer, as_dict=True,
		)[0]
		out.update(invoices=cint(inv.n), paid_invoices=cint(inv.paid), outstanding=flt(inv.outstanding), last_billed_on=inv.last)
		out["last_paid_on"] = frappe.db.get_value(
			"Payment Entry", {"party_type": "Customer", "party": customer, "docstatus": 1, "payment_type": "Receive"}, "max(posting_date)"
		)
		out["lead"] = frappe.db.get_value("Customer", customer, "lead_name")
	else:
		out.update(invoices=None, paid_invoices=None, outstanding=None, last_billed_on=None, last_paid_on=None, lead=None)
	if not out["lead"] and not customer and leads_hit:
		out["lead"] = sorted(leads_hit)[-1]

	opp = None
	if customer:
		filters = [{"party_name": customer}]
		if out["lead"]:
			filters.append({"opportunity_from": "Lead", "party_name": out["lead"]})
		for f in filters:
			opp = frappe.db.get_value("Opportunity", f, ["name", "sales_stage", "status"], as_dict=True, order_by="creation desc")
			if opp:
				break
	if not opp and not customer and opps_hit:
		opp = frappe.db.get_value("Opportunity", sorted(opps_hit)[-1], ["name", "sales_stage", "status"], as_dict=True)
	out["opportunity"] = opp.name if opp else None
	out["sales_stage"] = f"{opp.sales_stage} ({opp.status})" if opp else None
	return out


# ---------------------------------------------------------------- classification

def _classify(row: dict) -> tuple[str, str]:
	"""A hint, computed from the observed signals only. Returns (class, reason)."""
	if row.get("site_dir") == "archived":
		return "archived_by_hand", "directory under archived/sites"
	if row.get("probe_error"):
		return "unclassified", f"probe failed: {row['probe_error'][:80]}"
	sub = row.get("sub_status")
	outstanding = flt(row.get("outstanding"))
	last_paid = row.get("last_paid_on")
	paid_recently = bool(last_paid) and getdate(last_paid) >= getdate(add_days(nowdate(), -PAID_RECENTLY_DAYS))
	last_login = row.get("last_login")
	active_recently = bool(last_login) and getdate(str(last_login)[:10]) >= getdate(add_days(nowdate(), -30))
	used = cint(row.get("invoice_count")) or cint(row.get("master_data_count")) or last_login
	if sub == "Active" and (outstanding == 0 or paid_recently):
		return "paying", f"subscription Active, outstanding {outstanding:.0f}" + (", paid recently" if paid_recently else "") + ("" if used else " — site never used")
	if outstanding > 0:
		return "debtor", f"subscription {sub or '—'}, outstanding {outstanding:.0f}" + (", still using the site" if active_recently else "")
	if sub in ("Cancelled", "Past Due Date", "Unpaid"):
		return "cancelled_paid_up", f"subscription {sub}, nothing owed" + (", still using the site" if active_recently else "") + ("" if used else ", site never used")
	if not row.get("customer"):
		return "unmatched_site", "no control-site record matched" + (f"; {cint(row.get('invoice_count'))} invoices, last login {str(last_login or '—')[:10]}")
	if not used:
		return "never_used", "no invoices, no own master data, no customer login"
	return "used_unsigned", f"{cint(row.get('invoice_count'))} invoices, last login {str(last_login or '—')[:10]}, no subscription ever"


# ---------------------------------------------------------------- the inventory

def _site_row(s: dict, ident: dict, http: str) -> dict:
	company = ident.get("company") or {}
	people = ident.get("people") or []
	usage = ident.get("usage") or {}
	hits = _match(s["site"], ident)
	customer, quality, conflicts = _pick(hits["customers"])
	control = _control_side(customer, hits["contracts"], hits["opportunities"], hits["leads"])
	if not customer and control.get("customer"):
		quality = "strong"
	keys = set()
	for kind in hits.values():
		for ks in kind.values():
			keys.update(ks)
	responsible = people[0] if people else {}
	row = {
		"site": s["site"], "site_dir": s["site_dir"], "maintenance_mode": s["maintenance_mode"], "http_status": http,
		"company_name": company.get("company_name"), "nuit": company.get("tax_id"),
		"company_email": company.get("email"), "company_phone": company.get("phone_no"),
		"responsible": " · ".join(str(v) for v in (responsible.get("full_name"), responsible.get("email"), responsible.get("mobile_no")) if v) or None,
		"users": usage.get("user_count"), "last_login": usage.get("last_login"),
		"invoice_count": usage.get("invoice_count"), "last_invoice_on": usage.get("last_invoice_date"),
		"invoice_count_30d": usage.get("invoice_count_30d"), "master_data_count": usage.get("master_data_count"),
		"last_doc_modified": usage.get("last_doc_modified"), "db_size_mb": ident.get("db_size_mb"),
		"last_backup_on": s.get("last_backup_on"), "probe_error": ident.get("error") or "",
		**control,
		"match_keys": ";".join(sorted(keys)), "match_quality": quality if control.get("customer") else "none",
		"conflicts": conflicts,
	}
	row["class"], row["class_reason"] = _classify(row)
	return row


def _control_only(matched: dict) -> list[dict]:
	"""Cloud records on the control site that no site claimed."""
	rows = []
	customers = frappe.db.sql(
		"""select distinct c.name, c.customer_name, c.tax_id, c.email_id, c.mobile_no, c.lead_name
		   from `tabCustomer` c
		   where exists (select 1 from `tabContract` k where k.party_type='Customer' and k.party_name=c.name and k.docstatus < 2)
		      or exists (select 1 from `tabSubscription` s where s.party_type='Customer' and s.party=c.name)
		      or exists (select 1 from `tabMZ Signup` g where g.customer=c.name)""",
		as_dict=True,
	)
	for c in customers:
		if c.name in matched["customers"]:
			continue
		k = frappe.db.get_value("Contract", {"party_type": "Customer", "party_name": c.name, "docstatus": ("<", 2)}, ["name", "is_signed", "mz_tenant"], as_dict=True, order_by="creation desc")
		sub = frappe.db.get_value("Subscription", {"party_type": "Customer", "party": c.name}, "status", order_by="creation desc")
		opp = frappe.db.get_value("Opportunity", {"party_name": c.name}, ["name", "sales_stage", "status"], as_dict=True, order_by="creation desc")
		if not opp and c.lead_name:
			opp = frappe.db.get_value("Opportunity", {"opportunity_from": "Lead", "party_name": c.lead_name}, ["name", "sales_stage", "status"], as_dict=True, order_by="creation desc")
		rows.append({
			"record": "Customer", "name": c.name, "company_name": c.customer_name, "nuit": c.tax_id, "email": c.email_id, "mobile": c.mobile_no,
			"contract": k.name if k else None, "is_signed": cint(k.is_signed) if k else None, "mz_tenant": k.mz_tenant if k else None,
			"sub_status": sub, "opportunity": opp.name if opp else None, "sales_stage": opp.sales_stage if opp else None,
			"opp_status": opp.status if opp else None, "lead": c.lead_name,
		})
		if opp:
			matched["opportunities"].add(opp.name)
		if c.lead_name:
			matched["leads"].add(c.lead_name)
	for o in frappe.get_all("Opportunity", filters={"sales_stage": ("like", "Cloud - %")}, fields=["name", "party_name", "opportunity_from", "customer_name", "contact_email", "contact_mobile", "sales_stage", "status"], order_by="creation"):
		if o.name in matched["opportunities"]:
			continue
		rows.append({
			"record": "Opportunity", "name": o.name, "company_name": o.customer_name, "nuit": None, "email": o.contact_email, "mobile": o.contact_mobile,
			"contract": None, "is_signed": None, "mz_tenant": None, "sub_status": None, "opportunity": o.name,
			"sales_stage": o.sales_stage, "opp_status": o.status, "lead": o.party_name if o.opportunity_from == "Lead" else None,
		})
		if o.opportunity_from == "Lead":
			matched["leads"].add(o.party_name)
	for lead in frappe.get_all("Lead", filters={"email_id": ("is", "set")}, fields=["name", "company_name", "email_id", "mobile_no", "status"], order_by="creation"):
		if lead.name in matched["leads"]:
			continue
		if not (frappe.db.exists("Opportunity", {"opportunity_from": "Lead", "party_name": lead.name}) or frappe.db.exists("MZ Signup", {"lead": lead.name}) or frappe.db.exists("Customer", {"lead_name": lead.name})):
			continue
		rows.append({
			"record": "Lead", "name": lead.name, "company_name": lead.company_name, "nuit": None, "email": lead.email_id, "mobile": lead.mobile_no,
			"contract": None, "is_signed": None, "mz_tenant": None, "sub_status": None, "opportunity": None,
			"sales_stage": None, "opp_status": lead.status, "lead": lead.name,
		})
	return rows


def build() -> dict:
	"""The three sheets as lists of dicts. No writes."""
	site_rows, matched = [], {"customers": set(), "opportunities": set(), "leads": set()}
	for s in _sites():
		ident = _probe_identity(s["site"]) if s["site_dir"] == "live" else {"error": "archived: not probed", "company": {}, "people": [], "usage": {}}
		http = _http_status(s["site"])
		row = _site_row(s, ident, http)
		site_rows.append(row)
		if row.get("customer"):
			matched["customers"].add(row["customer"])
		if row.get("opportunity"):
			matched["opportunities"].add(row["opportunity"])
		if row.get("lead"):
			matched["leads"].add(row["lead"])
	control_rows = _control_only(matched)
	summary = defaultdict(int)
	for r in site_rows:
		summary[f"class: {r['class']}"] += 1
		summary[f"match: {r['match_quality']}"] += 1
	for r in control_rows:
		summary[f"control_only: {r['record']}"] += 1
	return {"sites": site_rows, "control_only": control_rows, "summary": [{"key": k, "count": v} for k, v in sorted(summary.items())]}


def to_xlsx(data: dict) -> bytes:
	from openpyxl import Workbook
	from openpyxl.utils import get_column_letter

	wb = Workbook()
	first = True
	for title, columns in (("sites", SITE_COLUMNS), ("control_only", CONTROL_COLUMNS), ("summary", ("key", "count"))):
		ws = wb.active if first else wb.create_sheet()
		first = False
		ws.title = title
		ws.append(list(columns))
		for r in data[title]:
			ws.append([_cell(r.get(c)) for c in columns])
		ws.freeze_panes = "B2"
		for i, c in enumerate(columns, 1):
			ws.column_dimensions[get_column_letter(i)].width = max(12, min(40, len(c) + 4))
	buf = io.BytesIO()
	wb.save(buf)
	return buf.getvalue()


def _cell(v):
	if v is None or isinstance(v, (int, float, str)):
		return v
	return str(v)


def inventory(attach: int = 1) -> dict:
	"""Build the inventory, attach it to MZ SaaS Settings, print the URL and the counts."""
	data = build()
	name = f"tenant_inventory_{nowdate().replace('-', '')}.xlsx"
	url = None
	if cint(attach):
		for old in frappe.get_all("File", filters={"file_name": name, "attached_to_doctype": "MZ SaaS Settings"}, pluck="name"):
			frappe.delete_doc("File", old, ignore_permissions=True, force=True)
		f = frappe.get_doc({
			"doctype": "File", "file_name": name, "is_private": 1,
			"attached_to_doctype": "MZ SaaS Settings", "attached_to_name": "MZ SaaS Settings",
			"content": to_xlsx(data),
		}).insert(ignore_permissions=True)
		frappe.db.commit()
		url = frappe.utils.get_url(f.file_url)
	print("\n".join(f"{r['key']:<32}{r['count']:>5}" for r in data["summary"]))
	if url:
		print(f"\n{url}")
	return {"file_url": url, "summary": data["summary"], "sites": len(data["sites"]), "control_only": len(data["control_only"])}
