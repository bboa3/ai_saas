"""SISLOG Multipay HTTP client.

API spec: P17023X — MPAG@SISLOG REST API Reference (SISLOG_20240803)

Reference request:  POST {base_url}/mobile/reference/request
Status query:       POST {base_url}/mobile/status/get  (Option 1, polling fallback)

Amount encoding: value * 100, rounded to int, formatted as string.
  Example: 50.00 MZN → "5000"

transactionId: max 22 chars, alphanumeric only.
"""

import json

import frappe
import requests


def _settings():
	s = frappe.get_single("Multipay Settings")
	if not s.enabled:
		frappe.throw("Multipay não está activado. Configure em Multipay Settings.", frappe.ValidationError)
	if not s.base_url or not s.api_key or not s.username:
		frappe.throw("Multipay Settings incompleto: base_url, api_key e username são obrigatórios.", frappe.ValidationError)
	return s


def _headers(api_key: str) -> dict:
	return {"apikey": api_key, "content-type": "application/json"}


def _fmt_amount(amount_mzn: float) -> str:
	"""Convert MZN float to SISLOG value string (2 implied decimals, no separator)."""
	return str(round(amount_mzn * 100))


def request_reference(transaction_id: str, amount_mzn: float, cel: str | None = None) -> dict:
	"""Call /mobile/reference/request and return the parsed response dict.

	Raises frappe.ValidationError on SISLOG error or HTTP failure.
	"""
	s = _settings()
	url = f"{s.base_url.rstrip('/')}/mobile/reference/request"
	body: dict = {
		"username": s.username,
		"transactionId": transaction_id,
		"value": _fmt_amount(amount_mzn),
	}
	if cel:
		body["cel"] = cel

	try:
		resp = requests.post(url, headers=_headers(s.get_password("api_key")), json=body, timeout=30)
		resp.raise_for_status()
		data = resp.json()
	except requests.RequestException as exc:
		frappe.log_error(title="Multipay: request_reference HTTP error", message=str(exc))
		frappe.throw(f"Falha ao contactar SISLOG: {exc}", frappe.ValidationError)

	if (data.get("status") or "").lower() != "valid":
		msg = data.get("errorMessage") or "Resposta inválida do SISLOG"
		frappe.log_error(title="Multipay: reference request failed", message=json.dumps(data))
		frappe.throw(f"SISLOG recusou o pedido: {msg}", frappe.ValidationError)

	if not data.get("entity") or not data.get("reference"):
		frappe.log_error(title="Multipay: missing entity/reference in response", message=json.dumps(data))
		frappe.throw("SISLOG retornou resposta válida mas sem entidade ou referência.", frappe.ValidationError)

	return data


def get_sislog_status(date_start: str | None = None, date_end: str | None = None) -> list[dict]:
	"""Call /mobile/status/get and return the entries list (Option 1 polling fallback).

	date_start / date_end format: yyyymmddHHMMSS
	"""
	s = _settings()
	url = f"{s.base_url.rstrip('/')}/mobile/status/get"
	body: dict = {"username": s.username}
	if date_start:
		body["dateStart"] = date_start
	if date_end:
		body["dateEnd"] = date_end

	try:
		resp = requests.post(url, headers=_headers(s.get_password("api_key")), json=body, timeout=30)
		resp.raise_for_status()
		data = resp.json()
	except requests.RequestException as exc:
		frappe.log_error(title="Multipay: get_sislog_status HTTP error", message=str(exc))
		return []

	if (data.get("status") or "").lower() != "valid":
		frappe.log_error(title="Multipay: status query failed", message=json.dumps(data))
		return []

	return data.get("entries") or []
