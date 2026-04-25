import hashlib
import hmac

import frappe


def generate_payment_token(invoice_name: str) -> str:
	"""HMAC-SHA256 token for payment page URL. Uses same pattern as erpnext_mz QR code."""
	secret = (frappe.local.conf.get("encryption_key") or "").encode("utf-8")
	message = f"pay|{invoice_name}".encode("utf-8")
	return hmac.new(secret, message, hashlib.sha256).hexdigest()[:16]


def validate_payment_token(invoice_name: str, token: str) -> bool:
	"""Constant-time comparison to prevent timing attacks."""
	if not token:
		return False
	expected = generate_payment_token(invoice_name)
	return hmac.compare_digest(expected, token)
