"""One-time migration: load segment_map.data.json into Segment Intelligence Map DocType."""

import json

import frappe

DATA_FILE = "/srv/frappe/frappe-bench/segment_map.data.json"

# Data uses lowercase; DocType Select option uses mixed case
TIPO_EXPERIENCIA_MAP = {
	"implementacao_assistida": "Implementacao_assistida",
	"rapido_padrao": "rapido_padrao",
	"premium_personalizado": "premium_personalizado",
}

# Data uses plural lowercase category keys; DocType uses singular capitalised
DORE_TIPO_MAP = {
	"operacionais": "Operacional",
	"financeiras": "Financeira",
	"fiscais": "Fiscal",
	"comerciais": "Comercial",
}


def run():
	with open(DATA_FILE) as f:
		data = json.load(f)

	records = data if isinstance(data, list) else list(data.values())
	created = skipped = errors = 0

	for r in records:
		perfil = r.get("perfil_tipo_cliente", {})
		atributos = r.get("atributos_base_negocio", {})
		dores = r.get("dores_estruturadas", {})
		impacto = r.get("impacto_financeiro", {})
		fit = r.get("erpnext_fit", {})
		qualificacao = r.get("qualificacao_comercial", {})
		oportunidades = r.get("oportunidades_upsell", []) or []
		gatilhos = r.get("gatilhos_acao", []) or []

		segmento = perfil.get("segmento")
		if not segmento:
			print("SKIP: record has no segmento")
			skipped += 1
			continue

		if frappe.db.exists("Segment Intelligence Map", segmento):
			print(f"SKIP (exists): {segmento}")
			skipped += 1
			continue

		# --- scalar fields ---
		perda = impacto.get("perda_mensal_estimada_mzn") or []

		# --- child tables: single-column lists ---
		tipos_negocio_cobertos = [{"tipo": v} for v in perfil.get("tipos_negocio_cobertos") or []]
		modelo_negocio = [{"modelo": v} for v in perfil.get("modelo_negocio") or []]
		modelo_receita = [{"modelo": v} for v in perfil.get("modelo_receita") or []]
		tags_busca_semantica = [{"tag": v} for v in perfil.get("tags_busca_semantica") or []]
		conformidade_mz = [{"conformidade": v} for v in impacto.get("conformidade_mz_necessaria") or []]
		modulos_minimos = [{"modulo": v} for v in fit.get("modulos_minimos") or []]
		modulos_upsell = [{"modulo": v} for v in fit.get("modulos_upsell") or []]
		configuracoes_criticas = [{"configuracao": v} for v in fit.get("configuracoes_criticas") or []]
		integracoes_recomendadas = [{"integracao": v} for v in fit.get("integracoes_recomendadas") or []]
		customizacoes_especificas = [{"customizacao": v} for v in fit.get("customizacoes_especificas") or []]
		red_flags = [{"descricao": v} for v in qualificacao.get("red_flags") or []]
		green_flags = [{"descricao": v} for v in qualificacao.get("green_flags") or []]
		objecoes_frequentes = [{"objecao": v} for v in qualificacao.get("objecoes_frequentes") or []]
		argumentos_chave_comerciais = [{"argumento": v} for v in qualificacao.get("argumentos_chave_comerciais") or []]

		# --- tags_diferenciadoras: {tipo: [tag, ...]} ---
		tags_diferenciadoras = [
			{"tipo": tipo, "tags": "\n".join(tags)}
			for tipo, tags in (perfil.get("tags_diferenciadoras_por_tipo") or {}).items()
		]

		# --- dores_estruturadas: {categoria: [{descricao, severidade_1_5, frequencia}]} ---
		dores_estruturadas = []
		for categoria, items in (dores or {}).items():
			tipo_dore = DORE_TIPO_MAP.get(categoria, categoria.capitalize())
			for item in (items or []):
				dores_estruturadas.append({
					"tipo_dore": tipo_dore,
					"descricao": item.get("descricao"),
					"severidade_1_5": item.get("severidade_1_5"),
					"frequencia": item.get("frequencia"),
				})

		# --- oportunidades_upsell ---
		oportunidades_upsell = []
		for o in oportunidades:
			faixa = o.get("faixa_preco_mzn") or []
			pre_req = o.get("pre_requisitos_sistema") or []
			oportunidades_upsell.append({
				"id_upsell": o.get("id_upsell"),
				"categoria": o.get("categoria"),
				"titulo": o.get("titulo"),
				"proposta_comercial_resumida": o.get("proposta_comercial_resumida"),
				"roi_estimado_meses": o.get("roi_estimado_meses"),
				"preco_min_mzn": faixa[0] if len(faixa) > 0 else None,
				"preco_max_mzn": faixa[1] if len(faixa) > 1 else None,
				"confianca_match_1_5": o.get("confianca_match_1_5"),
				"pre_requisitos_sistema": "\n".join(pre_req) if isinstance(pre_req, list) else pre_req,
			})

		# --- gatilhos_acao ---
		gatilhos_acao = [
			{
				"id_gatilho": g.get("id_gatilho"),
				"tipo": g.get("tipo"),
				"condicao_maquina": g.get("condicao_maquina"),
				"fonte_dados": g.get("fonte_dados"),
				"n8n_workflow_id": g.get("n8n_workflow_id"),
				"acao_comercial": g.get("acao_comercial"),
				"prioridade_execucao": g.get("prioridade_execucao"),
			}
			for g in gatilhos
		]

		try:
			doc = frappe.get_doc({
				"doctype": "Segment Intelligence Map",
				# perfil
				"segmento": segmento,
				"tipo_experiencia_implantacao": TIPO_EXPERIENCIA_MAP.get(
					perfil.get("tipo_experiencia_implantacao"), perfil.get("tipo_experiencia_implantacao")
				),
				"descricao_operacional": perfil.get("descricao_operacional"),
				# atributos
				"complexidade_operacional": atributos.get("complexidade_operacional_1_5"),
				"intensidade_financeira": atributos.get("intensidade_financeira_1_5"),
				"dependencia_stock": atributos.get("dependencia_stock_1_5"),
				"recorrencia_faturacao": atributos.get("recorrencia_faturacao"),
				# fit / plano
				"plano_recomendado": fit.get("plano_recomendado"),
				# qualificacao
				"maturidade_digital": qualificacao.get("maturidade_digital"),
				"score_prioridade": qualificacao.get("score_prioridade_1_10"),
				# impacto
				"risco_fiscal_mz": impacto.get("risco_fiscal_mz"),
				"custos_ocultos_severidade": impacto.get("custos_ocultos_severidade_1_5"),
				"perda_mensal_estimada_min": perda[0] if len(perda) > 0 else None,
				"perda_mensal_estimada_max": perda[1] if len(perda) > 1 else None,
				# child tables
				"conformidade_mz": conformidade_mz,
				"tipos_negocio_cobertos": tipos_negocio_cobertos,
				"modelo_negocio": modelo_negocio,
				"modelo_receita": modelo_receita,
				"tags_busca_semantica": tags_busca_semantica,
				"tags_diferenciadoras": tags_diferenciadoras,
				"modulos_minimos": modulos_minimos,
				"modulos_upsell": modulos_upsell,
				"configuracoes_criticas": configuracoes_criticas,
				"integracoes_recomendadas": integracoes_recomendadas,
				"customizacoes_especificas": customizacoes_especificas,
				"dores_estruturadas": dores_estruturadas,
				"oportunidades_upsell": oportunidades_upsell,
				"gatilhos_acao": gatilhos_acao,
				"red_flags": red_flags,
				"green_flags": green_flags,
				"objecoes_frequentes": objecoes_frequentes,
				"argumentos_chave_comerciais": argumentos_chave_comerciais,
			})
			doc.insert(ignore_permissions=True)
			print(f"CREATED: {segmento}")
			created += 1
		except Exception:
			print(f"ERROR: {segmento}")
			print(frappe.get_traceback())
			errors += 1

	frappe.db.commit()
	print(f"\nDone — created: {created}, skipped: {skipped}, errors: {errors}")
