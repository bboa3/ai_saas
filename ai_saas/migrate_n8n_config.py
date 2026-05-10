"""One-time migration: load segment_map.ai_n8n_config.json into AI N8N Configuration (Single DocType)."""

import json
import frappe

DATA_FILE = "/srv/frappe/frappe-bench/segment_map.ai_n8n_config.json"


def run():
	with open(DATA_FILE) as f:
		data = json.load(f)

	scoring = data.get("scoring_engine", {})
	weights = scoring.get("weights", {})
	thresholds = scoring.get("thresholds", {})
	normalization = scoring.get("normalization", {})
	error = data.get("error_handling", {})
	missing = error.get("missing_data_routing", {})
	prompts = (data.get("ai_configuration") or {}).get("prompts", {})

	doc = frappe.get_single("AI N8N Configuration")

	# Scoring weights
	doc.weight_severidade_dor = weights.get("severidade_dor", 0.30)
	doc.weight_impacto_financeiro = weights.get("impacto_financeiro", 0.25)
	doc.weight_gap_erpnext = weights.get("gap_erpnext", 0.20)
	doc.weight_maturidade_cliente = weights.get("maturidade_cliente", 0.15)
	doc.weight_fit_fiscal_mz = weights.get("fit_fiscal_mz", 0.10)

	# Normalization map (store as JSON, excluding the formula-only escala_1_5 key)
	norm_clean = {k: v for k, v in normalization.items() if k != "escala_1_5"}
	doc.normalization_map = json.dumps(norm_clean, ensure_ascii=False, indent=2)

	# Thresholds
	doc.threshold_upsell_premium = thresholds.get("upsell_premium", 8.0)
	doc.threshold_upsell_standard = thresholds.get("upsell_standard", 6.0)
	doc.threshold_triagem_necessaria = thresholds.get("triagem_necessaria", 4.0)

	# Routing rules
	doc.routing_rules = json.dumps(data.get("routing_rules", []), ensure_ascii=False, indent=2)

	# Error handling
	doc.default_workflow = error.get("default_workflow")
	doc.escalation_contact = error.get("escalation_contact")
	doc.missing_data_workflow = missing.get("workflow")
	doc.missing_data_notification = missing.get("notification")

	# AI prompts
	doc.ai_prompt_proposta_comercial = prompts.get("proposta_comercial")
	doc.ai_prompt_email_ativacao = prompts.get("email_ativacao")
	doc.ai_prompt_fallback = prompts.get("fallback_proposal")

	doc.save(ignore_permissions=True)
	frappe.db.commit()
	print("AI N8N Configuration updated successfully.")
