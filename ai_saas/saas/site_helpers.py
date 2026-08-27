"""
Functions called via `bench --site <new-site> execute` inside a tenant site.

They run in that site's Frappe context, so they can use Frappe utilities
(generate_hash, get_url, db operations) without any cross-site concerns; bench
execute commits after the function returns, so no explicit commit is needed.

Because ai_saas is *not* installed on tenant sites, provisioning reaches this
module through an `__import__(...)` expression (provisioning.py) rather than by
method name. Code that a tenant should own belongs in erpnext_mz instead — the
usage probe lives there, in erpnext_mz.utils.tenant_usage.
"""
import frappe


def generate_user_reset_link(email: str) -> str:
	"""Generate a password-reset link for the given user on this site.

	Stores the SHA-256 hash of the key in the User record (matching Frappe's own
	reset_password() convention) and returns the full https reset URL so the caller
	can embed it in the welcome email.

	bench execute prints the return value as JSON, so the caller reads it from stdout.
	"""
	from frappe.utils import get_url
	from frappe.utils.data import sha256_hash

	key = frappe.generate_hash()
	hashed_key = sha256_hash(key)

	# In Frappe, User.name == the user's email address.
	frappe.db.set_value(
		"User",
		email,
		{
			"reset_password_key": hashed_key,
			"last_reset_password_key_generated_on": frappe.utils.now_datetime(),
		},
		update_modified=False,
	)

	url = f"/update-password?key={key}"
	return get_url(url, allow_header_override=False)
