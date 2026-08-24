"""The language a provisioned tenant ends up with.

Provisioning sets the site to pt-MZ and then runs Frappe's setup wizard, which writes
System Settings from its own arguments.  Two things have to hold for the tenant to land
in Portuguese: the wizard has to be handed a language it can actually resolve, and the
Mozambique defaults have to be applied after it rather than before.
"""

from __future__ import annotations

import unittest

import frappe

from ai_saas.saas import provisioning
from ai_saas.saas.provisioning import (
	MZ_LANGUAGE_CODE,
	MZ_LANGUAGE_NAME,
	PROVISIONING_STEPS,
	build_wizard_args,
)


class _Prov:
	"""The handful of fields build_wizard_args() reads off the provisioning record."""

	site_name = "tenant.erp.mozeconomia.co.mz"
	contact_email = "cliente@exemplo.co.mz"
	customer_name = "Empresa Exemplo"
	name = "PROV-TEST"


class TestWizardLanguage(unittest.TestCase):
	def setUp(self):
		from frappe.desk.page.setup_wizard.setup_wizard import get_language_code

		self.resolve = get_language_code
		if not frappe.db.exists("Language", MZ_LANGUAGE_CODE):
			self.skipTest("Language pt-MZ não existe neste site")

	def test_the_wizard_resolves_the_name_we_pass(self):
		self.assertEqual(self.resolve(MZ_LANGUAGE_NAME), MZ_LANGUAGE_CODE)

	def test_and_would_not_have_resolved_the_code(self):
		"""Why the constant is the name and not the code: the wizard's own
		get_language_code() matches on `language_name` alone, and an unresolved
		language makes update_system_settings fall back to "en"."""
		self.assertIsNone(self.resolve(MZ_LANGUAGE_CODE))

	def test_both_keys_the_wizard_reads_carry_it(self):
		"""`language` is read by update_system_settings, `lang` by update_global_settings,
		which calls set_default_language(get_language_code(args.lang))."""
		args = self._args()
		self.assertEqual(args["language"], MZ_LANGUAGE_NAME)
		self.assertEqual(args["lang"], MZ_LANGUAGE_NAME)

	def test_the_rest_of_the_country_defaults_travel_with_it(self):
		args = self._args()
		self.assertEqual(args["country"], "Mozambique")
		self.assertEqual(args["currency"], "MZN")
		self.assertEqual(args["timezone"], "Africa/Maputo")

	def _args(self) -> dict:
		from unittest.mock import patch

		with patch.object(provisioning, "_get_contact_password", return_value="x"):
			return build_wizard_args(_Prov())


class TestStepOrder(unittest.TestCase):
	def _index(self, name: str) -> int:
		names = [step.__name__ for step in PROVISIONING_STEPS]
		self.assertIn(name, names)
		return names.index(name)

	def test_the_language_record_exists_before_the_wizard_runs(self):
		"""The wizard resolves by `language_name`; with no Language record the lookup
		returns None and it writes "en"."""
		self.assertLess(self._index("_step_ensure_language"), self._index("_step_run_setup_wizard"))

	def test_the_system_defaults_are_applied_after_the_wizard(self):
		"""update_system_settings overwrites language, country, currency, time zone and
		the date and number formats with its own values, so anything applied before it
		is lost."""
		self.assertGreater(
			self._index("_step_apply_system_settings"), self._index("_step_run_setup_wizard")
		)

	def test_the_company_profile_is_seeded_after_the_company_exists(self):
		self.assertGreater(
			self._index("_step_seed_company_profile"), self._index("_step_run_setup_wizard")
		)
