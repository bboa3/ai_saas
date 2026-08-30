import frappe
from frappe.utils import getdate, nowdate

from ai_saas.saas import crm

# The two triggers of the funnel, split along the signature (docs/sales-funnel-implementation.md, B1):
#   contract submitted -> the site is created and the trial begins, signed or not;
#   contract signed    -> the subscription is created and billing begins.
# The account phase is never stored: tenant_lifecycle.account_phase derives it from
# the signature and the site's status, and the Opportunity records the sales stage.


def on_contract_submitted(doc, method=None):
	"""Contract on_submit: provision the tenant site regardless of signature.

	The mz_subscription_plan and mz_tenant gates stay exactly as before the
	trigger split — no plan or no subdomain still means no provisioning.
	A contract submitted already signed (the manual sales path) also gets its
	Subscription here, producing exactly the pre-split result.
	"""
	if doc.party_type != "Customer":
		return
	if not doc.get("mz_subscription_plan"):
		return
	if doc.is_signed and not doc.get("mz_linked_subscription"):
		_setup_subscription(doc)
	if doc.is_signed:
		_move_customer_to_commercial_group(doc)
		crm.report_for_contract(doc.name, crm.STAGE_ACTIVATED, status="Converted")
	_maybe_provision_tenant(doc)


def on_contract_signed(doc, method=None):
	"""Contract on_update_after_submit: create the Subscription on the is_signed 0->1 transition.

	Fires for every update-after-submit, so it must decide two things:
	- B3 guard: the plan is editable after submit, but only while no Subscription is linked;
	- signature detection: act only when is_signed flipped 0->1 in this save (compared via
	  get_doc_before_save(); when the before-image is unavailable the mz_linked_subscription
	  guard alone keeps this idempotent).
	"""
	if doc.party_type != "Customer":
		return

	before = doc.get_doc_before_save()

	# B3: never swap the plan under a live subscription.
	if (
		before is not None
		and doc.get("mz_linked_subscription")
		and (before.get("mz_subscription_plan") or "") != (doc.get("mz_subscription_plan") or "")
	):
		frappe.throw(
			"O plano não pode ser alterado depois de a subscrição existir. "
			"Cancele a subscrição ligada antes de mudar o plano."
		)

	if not doc.is_signed:
		return
	if before is not None and before.is_signed:
		return  # already signed before this save — not a signature transition
	if not doc.get("mz_subscription_plan"):
		return

	if not doc.get("mz_linked_subscription"):
		_setup_subscription(doc)
	_move_customer_to_commercial_group(doc)
	_apply_scheduler_policy(doc)
	# The signature is the funnel's conversion: the Opportunity closes as won.
	crm.report_for_contract(doc.name, crm.STAGE_ACTIVATED, status="Converted")
	# Legacy safety net: a contract submitted before the trigger split never provisioned
	# unsigned. provision_tenant is idempotent (one record per contract), so this can
	# never create a second site.
	_maybe_provision_tenant(doc)


def _apply_scheduler_policy(doc):
	"""The tenant scheduler runs only for plans listed in MZ SaaS Settings.scheduler_plans. The
	plan can be corrected at activation, so the policy is re-applied at signature. A
	failing bench command must not undo the signature: log it and tell ops."""
	if not doc.get("mz_tenant"):
		return
	try:
		from ai_saas.saas.provisioning import apply_scheduler_policy

		apply_scheduler_policy(doc.name)
	except Exception:
		frappe.log_error(title=f"AI SaaS: scheduler policy not applied ({doc.name})", message=frappe.get_traceback())
		from ai_saas.saas.alerts import notify_ops

		notify_ops(f"Política do agendador não aplicada: {doc.name}",
		           "<p>O contrato foi assinado mas o estado do agendador do site não foi actualizado. Aplicar à mão: "
		           "<code>bench --site &lt;site&gt; enable-scheduler|disable-scheduler</code>.</p>")


def _move_customer_to_commercial_group(doc):
	"""A4/E1: a signed customer leaves the trial group so sales reporting stops
	counting it as a trial. Target: MZ SaaS Settings.commercial_customer_group,
	else Selling Settings' default group; if neither is set, leave it alone."""
	from ai_saas.install import TRIAL_CUSTOMER_GROUP
	from ai_saas.saas.tenant_lifecycle import get_settings

	if frappe.db.get_value("Customer", doc.party_name, "customer_group") != TRIAL_CUSTOMER_GROUP:
		return
	target = get_settings().commercial_customer_group or frappe.db.get_single_value(
		"Selling Settings", "customer_group"
	)
	if target and target != TRIAL_CUSTOMER_GROUP and frappe.db.exists("Customer Group", target):
		frappe.db.set_value("Customer", doc.party_name, "customer_group", target)


def _maybe_provision_tenant(doc):
	"""Queue tenant site provisioning if mz_tenant is set. Idempotent."""
	if not doc.get("mz_tenant"):
		return
	try:
		from ai_saas.saas.provisioning import provision_tenant
		provision_tenant(doc.name)
	except frappe.ValidationError:
		raise  # surface invalid-slug errors to the user immediately
	except Exception:
		frappe.log_error(
			title=f"AI SaaS: Falha ao enfileirar provisionamento para {doc.name}",
			message=frappe.get_traceback(),
		)


def on_contract_cancel(doc, method=None):
	"""Cancel the linked Subscription when the contract is cancelled."""
	sub_name = doc.get("mz_linked_subscription")
	if not sub_name or not frappe.db.exists("Subscription", sub_name):
		return
	try:
		sub = frappe.get_doc("Subscription", sub_name)
		if sub.status != "Cancelled":
			sub.cancel_subscription()
			sub.save(ignore_permissions=True)
			frappe.db.commit()
	except Exception:
		frappe.log_error(
			title=f"AI SaaS: Failed to cancel Subscription {sub_name}",
			message=frappe.get_traceback(),
		)



def _setup_subscription(doc):
	"""Create an ERPNext Subscription from the contract's selected Subscription Plan."""
	plan_name = doc.mz_subscription_plan
	if not frappe.db.exists("Subscription Plan", plan_name):
		frappe.log_error(
			title=f"AI SaaS: Subscription Plan '{plan_name}' not found",
			message=f"Contract: {doc.name}",
		)
		return

	company = _get_company()
	if not company:
		frappe.log_error(
			title="AI SaaS: No default company found",
			message=f"Contract: {doc.name} — Set a default company in Global Defaults.",
		)
		return

	billing_start = compute_billing_start(doc)

	sub = frappe.get_doc({
		"doctype": "Subscription",
		"party_type": "Customer",
		"party": doc.party_name,
		"company": company,
		"start_date": billing_start,
		"end_date": doc.end_date or None,
		"generate_invoice_at": "Beginning of the current subscription period",
		"submit_invoice": 1,
		"days_until_due": 7,
		"generate_new_invoices_past_due_date": 0,
		"sales_tax_template": _get_default_tax_template(company),
		"plans": [{"plan": plan_name, "qty": 1}],
	})
	# Subscription is not submittable — insert() is sufficient. The controller
	# auto-sets status="Active" during validate. No commit here: this runs inside
	# the Contract's own save, and a later failure in that save must roll it back.
	sub.insert(ignore_permissions=True)

	frappe.db.set_value(
		"Contract",
		doc.name,
		{"mz_linked_subscription": sub.name, "mz_billing_start": billing_start},
		update_modified=False,
	)
	doc.mz_linked_subscription = sub.name
	doc.mz_billing_start = billing_start
	_ensure_customer_primary_contact(doc)

	# E2: when billing starts today, issue the first invoice now instead of waiting
	# for the daily job — invoice generation is a strict equality on one date
	# (subscription.py can_generate_new_invoice), and a period whose start is
	# already past is never invoiced.
	if getdate(billing_start) == getdate(nowdate()):
		try:
			sub.process(posting_date=nowdate())
		except Exception:
			frappe.log_error(
				title=f"AI SaaS: first invoice for {sub.name} not generated",
				message=frappe.get_traceback(),
			)


def compute_billing_start(doc):
	"""E2: billing starts on the later of the contract start date and the signature
	date — signing early never costs money, signing late starts today."""
	today = getdate(nowdate())
	start = getdate(doc.start_date) if doc.start_date else today
	return max(start, today)


def _ensure_customer_primary_contact(doc):
	"""Safety net (B5): a Customer must leave the funnel pointing at its primary contact
	AND its primary address.

	ERPNext fetches Customer.email_id from the primary contact and Sales Invoice's
	contact_email from that, so without one no notification reaches the customer; print
	formats and party details read customer_primary_address. Contracts made in the desk,
	or before A4 existed, may have neither — both are resolved through Dynamic Link, the
	same way provisioning's _resolve_contact_email does. Whatever is already set stays.
	"""
	from ai_saas.saas.party import ensure_customer_primaries

	if doc.party_name:
		ensure_customer_primaries(doc.party_name)


def _get_company():
	"""Resolve the default company for subscription/invoice generation."""
	return (
		frappe.defaults.get_user_default("company")
		or frappe.db.get_single_value("Global Defaults", "default_company")
	)


def _get_default_tax_template(company):
	"""Return the default Sales Taxes and Charges Template for the company, or None."""
	return frappe.db.get_value(
		"Sales Taxes and Charges Template",
		{"company": company, "is_default": 1},
		"name",
	)
