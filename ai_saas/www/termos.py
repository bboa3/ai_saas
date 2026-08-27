"""Terms of service page: the Contract Template's text, rendered generically.
Route: /termos. Linked from /registo before an account or contract exists."""

import frappe

no_cache = 1
no_breadcrumbs = 1


def get_context(context):
	from ai_saas.install import _CONTRACT_TEMPLATE_TITLE

	context.title = "Termos de Serviço — MozEconomia Cloud"
	terms = frappe.db.get_value("Contract Template", _CONTRACT_TEMPLATE_TITLE, "contract_terms") or ""
	context.terms = frappe.render_template(terms, {
		"party_name": "[a sua empresa]", "mz_subscription_plan": "[o plano escolhido]",
		"mz_tenant_url": "[o-seu-endereco].erp.mozeconomia.co.mz", "start_date": frappe.utils.nowdate(),
	})
	return context
