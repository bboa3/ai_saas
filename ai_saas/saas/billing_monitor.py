import frappe
from frappe.utils import add_days, nowdate, getdate


def flag_overdue_customers():
	"""Daily: handle all overdue billing actions and pre-billing reminders."""
	_send_prebilling_reminders()
	overdue = _get_overdue_invoices()
	_create_overdue_reviews(overdue)
	_create_d7_followup_tasks(overdue)
	_process_d15_deactivations(overdue)
	frappe.db.commit()


# ---------------------------------------------------------------------------
# Pre-billing reminder (fires before invoice is created)
# ---------------------------------------------------------------------------

def _send_prebilling_reminders():
	"""Send a pre-billing email 4 days before the next invoice is generated.

	Queries active Subscriptions whose current_invoice_start is 4 days from
	today — i.e. the invoice has not been created yet.  For each, resolves
	the linked Contract and sends a templated email to contact_email.
	"""
	target_date = getdate(add_days(nowdate(), 4))

	subscriptions = frappe.db.get_all(
		"Subscription",
		filters={"status": "Active", "current_invoice_start": target_date},
		fields=["name", "party", "current_invoice_start", "current_invoice_end", "plans"],
	)

	for sub in subscriptions:
		contract = frappe.db.get_value(
			"Contract",
			{"mz_linked_subscription": sub.name},
			["name", "contact_email"],
			as_dict=True,
		)
		if not contract or not contract.contact_email:
			continue

		# Resolve plan details for estimated amount
		plan_name = frappe.db.get_value(
			"Subscription Plan Detail", {"parent": sub.name}, "plan"
		)
		plan = frappe.db.get_value(
			"Subscription Plan", plan_name, ["plan_name", "cost", "currency"], as_dict=True
		) if plan_name else None

		_send_prebilling_email(sub, contract, plan)


def _send_prebilling_email(sub, contract, plan):
	"""Send the pre-billing email for a single subscription."""
	from frappe.utils.formatters import format_value

	start = frappe.utils.formatdate(sub.current_invoice_start)
	end = frappe.utils.formatdate(sub.current_invoice_end)

	if plan and plan.cost:
		amount_line = format_value(plan.cost, {"fieldtype": "Currency", "currency": plan.currency})
		amount_text = f"<tr><td style='padding:4px 16px 4px 0'><strong>Valor Estimado:</strong></td><td>{amount_line}</td></tr>"
	else:
		amount_text = ""

	customer_name = frappe.db.get_value("Customer", sub.party, "customer_name") or sub.party
	message = f"""
<p>Prezado {customer_name},</p>
<p>Informamos que a sua fatura referente ao período <strong>{start} — {end}</strong> será emitida em <strong>4 dias</strong>.</p>
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
		subject=f"Aviso de Faturação — A sua fatura será emitida em 4 dias | MozEconomia Cloud",
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
		if frappe.db.exists(
			"MZ Overdue Review",
			{"contract": contract, "review_status": ["in", ["Pending Review", "Deactivate"]]},
		):
			continue

		frappe.get_doc({
			"doctype": "MZ Overdue Review",
			"customer": inv.customer,
			"contract": contract,
			"outstanding_amount": inv.outstanding_amount,
			"overdue_since": inv.due_date,
			"review_status": "Pending Review",
		}).insert(ignore_permissions=True)


def _create_d7_followup_tasks(invoices):
	"""D+7: create a sales ToDo task and a phone-call Event for follow-up."""
	d7_invoices = [inv for inv in invoices if inv.days_overdue >= 7]

	for inv in d7_invoices:
		if not inv.subscription:
			continue
		contract = frappe.db.get_value("Contract", {"mz_linked_subscription": inv.subscription}, "name")
		if not contract:
			continue

		# Deduplicate: skip if an open ToDo already exists for this contract
		if frappe.db.exists(
			"ToDo",
			{"reference_type": "Contract", "reference_name": contract, "status": "Open"},
		):
			continue

		customer_name = (
			frappe.db.get_value("Customer", inv.customer, "customer_name") or inv.customer
		)
		description = (
			f"Fatura {inv.name} em atraso há {inv.days_overdue} dias.\n"
			f"Valor em dívida: {inv.outstanding_amount} — Vencimento: {inv.due_date}\n"
			f"Contactar o cliente {customer_name} para regularização do pagamento."
		)

		frappe.get_doc({
			"doctype": "ToDo",
			"status": "Open",
			"priority": "High",
			"date": nowdate(),
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


def _process_d15_deactivations(invoices):
	"""D+15: move contract to Deactivation Queue if still unpaid."""
	d15_invoices = [inv for inv in invoices if inv.days_overdue >= 15]

	for inv in d15_invoices:
		if not inv.subscription:
			continue
		contract = frappe.db.get_value("Contract", {"mz_linked_subscription": inv.subscription}, "name")
		if not contract:
			continue

		if frappe.db.exists(
			"MZ Overdue Review",
			{"contract": contract, "review_status": "Deactivate"},
		):
			continue

		frappe.get_doc({
			"doctype": "MZ Overdue Review",
			"customer": inv.customer,
			"contract": contract,
			"outstanding_amount": inv.outstanding_amount,
			"overdue_since": inv.due_date,
			"review_status": "Deactivate",
			"notes": (
				f"Movido automaticamente para fila de desactivação após "
				f"{inv.days_overdue} dias de atraso."
			),
		}).insert(ignore_permissions=True)
