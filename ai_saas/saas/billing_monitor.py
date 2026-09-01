import frappe
from frappe.utils import add_days, cint, getdate, nowdate

from ai_saas.saas.settings import get_settings


def flag_overdue_customers():
	"""Daily: pre-billing reminders, the D+1 review row, and the commercial follow-up.

	Thresholds come from MZ SaaS Settings (F1). Suspension and archive belong to
	tenant_lifecycle.process_lifecycle — the old D+15 "deactivation queue" writer
	is retired (F5): its rows were read by nobody, while the engine acts on dates.
	"""
	_send_prebilling_reminders()
	overdue = _get_overdue_invoices()
	_create_overdue_reviews(overdue)
	_create_followup_tasks(overdue)
	frappe.db.commit()


# ---------------------------------------------------------------------------
# Pre-billing reminder (fires before invoice is created)
# ---------------------------------------------------------------------------

def _send_prebilling_reminders():
	"""Send a pre-billing email N days before the next invoice is generated.

	Queries active Subscriptions whose current_invoice_start is N days from
	today — i.e. the invoice has not been created yet.  For each, resolves
	the linked Contract and sends a templated email to contact_email.
	"""

	lead_days = get_settings().prebilling_reminder_days
	target_date = getdate(add_days(nowdate(), lead_days))

	subscriptions = frappe.db.get_all(
		"Subscription",
		filters={"status": "Active", "current_invoice_start": target_date},
		fields=["name", "party", "current_invoice_start", "current_invoice_end"],
	)

	for sub in subscriptions:
		contract = frappe.db.get_value(
			"Contract",
			{"mz_linked_subscription": sub.name},
			["name", "contact_email", "mz_users"],
			as_dict=True,
		)
		if not contract or not contract.contact_email:
			continue

		# Resolve plan details for the estimated amount. The plan cost is a per-user
		# rate (per-user pricing): the invoice total is cost x the plan row's qty.
		plan_row = frappe.db.get_value(
			"Subscription Plan Detail", {"parent": sub.name}, ["plan", "qty"], as_dict=True
		)
		plan = frappe.db.get_value(
			"Subscription Plan", plan_row.plan, ["plan_name", "cost", "currency"], as_dict=True
		) if plan_row else None
		qty = (cint(plan_row.qty) or 1) if plan_row else 1

		_send_prebilling_email(sub, contract, plan, qty, lead_days)


def _send_prebilling_email(sub, contract, plan, qty, lead_days):
	"""Send the pre-billing email for a single subscription."""
	from frappe.utils.formatters import format_value

	start = frappe.utils.formatdate(sub.current_invoice_start)
	end = frappe.utils.formatdate(sub.current_invoice_end)

	if plan and plan.cost:
		amount_line = format_value(plan.cost * qty, {"fieldtype": "Currency", "currency": plan.currency})
		if qty > 1:
			# First user included: seats = billed + 1 (the contract's mz_users when set).
			seats = cint(contract.get("mz_users")) or qty + 1
			unit_line = format_value(plan.cost, {"fieldtype": "Currency", "currency": plan.currency})
			amount_line = (
				f"{seats} utilizadores (1.º incluído): {qty} × {unit_line} = {amount_line}"  # noqa: RUF001 (customer-facing sign)
			)
		amount_text = f"<tr><td style='padding:4px 16px 4px 0'><strong>Valor Estimado:</strong></td><td>{amount_line}</td></tr>"
	else:
		amount_text = ""

	customer_name = frappe.db.get_value("Customer", sub.party, "customer_name") or sub.party
	message = f"""
<p>Prezado {customer_name},</p>
<p>Informamos que a sua fatura referente ao período <strong>{start} — {end}</strong> será emitida em <strong>{lead_days} dias</strong>.</p>
<table style="border-collapse:collapse;margin:12px 0">
  <tr><td style="padding:4px 16px 4px 0"><strong>Período de Serviço:</strong></td><td>{start} — {end}</td></tr>
  {amount_text}
</table>
<p>Assim que a fatura for emitida, receberá o documento em anexo no email habitual. O prazo de pagamento é de <strong>7 dias</strong> após a emissão.</p>
<p>Para qualquer esclarecimento, estamos sempre disponíveis:</p>
<ul>
  <li>WhatsApp: <strong>+258 87 4444 645</strong></li>
  <li>Email: <a href="mailto:contacto@mozeconomia.co.mz">contacto@mozeconomia.co.mz</a></li>
</ul>
<p>Com boas energias,<br><strong>Equipa MozEconomia Cloud</strong></p>
"""

	frappe.sendmail(
		recipients=[contract.contact_email],
		subject=f"Aviso de Faturação — A sua fatura será emitida em {lead_days} dias | MozEconomia Cloud",
		message=message,
		reference_doctype="Contract",
		reference_name=contract.name,
	)


# ---------------------------------------------------------------------------
# Overdue invoice helpers
# ---------------------------------------------------------------------------

def _get_overdue_invoices():
	"""Return submitted AI SaaS invoices with outstanding balance, with days_overdue."""
	return frappe.db.sql(
		"""
		SELECT name, customer, subscription, outstanding_amount, due_date,
		       DATEDIFF(CURDATE(), due_date) AS days_overdue
		FROM `tabSales Invoice`
		WHERE subscription IS NOT NULL
			AND outstanding_amount > 0
			AND due_date < CURDATE()
			AND docstatus = 1
		""",
		as_dict=True,
	)


def _create_overdue_reviews(invoices):
	"""D+1: create a Pending Review record the first time a subscription invoice goes overdue."""
	for inv in invoices:
		if not inv.subscription:
			continue
		contract = frappe.db.get_value("Contract", {"mz_linked_subscription": inv.subscription}, "name")
		if not contract:
			continue
		# One review per contract per overdue invoice, whatever state the team moved it to.
		if frappe.db.exists("MZ Overdue Review", {"contract": contract, "overdue_since": inv.due_date}):
			continue

		frappe.get_doc({
			"doctype": "MZ Overdue Review",
			"customer": inv.customer,
			"contract": contract,
			"outstanding_amount": inv.outstanding_amount,
			"overdue_since": inv.due_date,
			"review_status": "Pending Review",
		}).insert(ignore_permissions=True)


def _create_followup_tasks(invoices):
	"""Overdue follow-up: create a sales ToDo task and a phone-call Event."""

	threshold = get_settings().overdue_followup_days
	flagged = [inv for inv in invoices if inv.days_overdue >= threshold]

	for inv in flagged:
		if not inv.subscription:
			continue
		contract = frappe.db.get_value("Contract", {"mz_linked_subscription": inv.subscription}, "name")
		if not contract:
			continue

		# Deduplicate per (contract, invoice): the marker survives status changes, so a
		# closed or orphaned ToDo can never silence future follow-ups for other invoices.
		marker = f"[followup:{contract}:{inv.name}]"
		if frappe.db.exists("ToDo", {"reference_type": "Contract", "reference_name": contract,
		                             "description": ("like", f"%{marker}%")}):
			continue

		customer_name = (
			frappe.db.get_value("Customer", inv.customer, "customer_name") or inv.customer
		)
		description = (
			f"Fatura {inv.name} em atraso há {inv.days_overdue} dias.\n"
			f"Valor em dívida: {inv.outstanding_amount} — Vencimento: {inv.due_date}\n"
			f"Contactar o cliente {customer_name} para regularização do pagamento.\n"
			f"{marker}"
		)
		assignee = (
			frappe.db.get_value("Contract", contract, "mz_account_manager")
			or get_settings().default_sales_user
		)
		frappe.get_doc({
			"doctype": "ToDo",
			"status": "Open",
			"priority": "High",
			"date": nowdate(),
			"allocated_to": assignee,
			"reference_type": "Contract",
			"reference_name": contract,
			"description": description,
		}).insert(ignore_permissions=True)

		call_datetime = add_days(nowdate(), 1) + " 09:00:00"
		frappe.get_doc({
			"doctype": "Event",
			"subject": f"Chamada: {customer_name} — Fatura {inv.name} em atraso",
			"event_category": "Call",
			"event_type": "Private",
			"starts_on": call_datetime,
			"description": description,
		}).insert(ignore_permissions=True)
