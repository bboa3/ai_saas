"""The one-line Mozambican address parser behind the signup's Endereço field."""

import frappe
from frappe.tests.utils import FrappeTestCase

from ai_saas.saas.mz_address import parse_mz_address


class TestMzAddress(FrappeTestCase):
	def test_full_address(self):
		p = parse_mz_address("Av. 25 de Setembro, 1234, Bairro Central, Maputo")
		self.assertEqual(p, {"address_line1": "Av. 25 de Setembro, 1234", "address_line2": "Bairro Central",
		                     "city": "Maputo", "state": "Maputo Cidade"})

	def test_quarteirao_casa_and_province(self):
		p = parse_mz_address("Rua da Resistência, Q. 12, Casa 5, B. Munhava, Beira, Sofala")
		self.assertEqual((p["address_line1"], p["address_line2"], p["city"], p["state"]),
		                 ("Rua da Resistência, Q. 12, Casa 5", "B. Munhava", "Beira", "Sofala"))

	def test_city_only_and_unknown_city(self):
		self.assertEqual(parse_mz_address("Matola"), {"address_line1": "Matola", "address_line2": "", "city": "Matola", "state": "Maputo"})
		p = parse_mz_address("Rua 3, Vila Nova")
		self.assertEqual((p["address_line1"], p["city"], p["state"]), ("Rua 3", "Vila Nova", ""))

	def test_nothing_is_lost(self):
		p = parse_mz_address("  Estrada Nacional 1 ; km 12,  Chimoio , Manica ")
		self.assertEqual((p["address_line1"], p["city"], p["state"]), ("Estrada Nacional 1 ; km 12", "Chimoio", "Manica"))
		self.assertEqual(parse_mz_address(""), {"address_line1": "", "address_line2": "", "city": "", "state": ""})

	# ---- one line, no commas: what people actually type --------------------------

	def test_city_written_with_its_administrative_wrapper(self):
		for text in ("Cidade de maputo", "MAPUTO", "cidade da Beira", "xai xai"):
			p = parse_mz_address(text)
			self.assertTrue(p["city"], f"{text!r} should name a city")
			self.assertTrue(p["address_line1"], f"{text!r} must still fill address_line1 (mandatory)")
		self.assertEqual(parse_mz_address("Cidade de maputo")["city"], "Maputo")     # canonical spelling
		self.assertEqual(parse_mz_address("cidade da Beira")["state"], "Sofala")
		self.assertEqual(parse_mz_address("xai xai")["city"], "Xai-Xai")

	def test_city_found_inside_a_line_without_commas(self):
		p = parse_mz_address("Av. 25 de Setembro Maputo")
		self.assertEqual((p["address_line1"], p["city"]), ("Av. 25 de Setembro", "Maputo"))
		p = parse_mz_address("Bairro Munhava Beira")
		# Only a bairro and a city: the bairro becomes the mandatory line1, nothing invented.
		self.assertEqual((p["address_line1"], p["address_line2"], p["city"], p["state"]),
		                 ("Bairro Munhava", "", "Beira", "Sofala"))
		p = parse_mz_address("Rua da Paz nr 4 Vilanculos")
		self.assertEqual(p["city"], "Vilankulo")                                     # alias → canonical

	def test_a_street_alone_names_no_city_and_a_bairro_is_never_one(self):
		# The caller must ask; guessing would put a street name on the invoice as a city.
		self.assertEqual(parse_mz_address("Av. 25 de Setembro")["city"], "")
		p = parse_mz_address("Av. 25 de Setembro, Bairro Central")
		self.assertEqual((p["address_line1"], p["address_line2"], p["city"]), ("Av. 25 de Setembro", "Bairro Central", ""))

	def test_address_line1_is_never_empty_when_anything_was_typed(self):
		p = parse_mz_address("Bairro Central, Maputo")
		self.assertEqual((p["address_line1"], p["address_line2"], p["city"]), ("Bairro Central", "", "Maputo"))

	# ---- the same parser normalises what provisioning hands to the tenant --------

	def test_provisioning_fills_gaps_from_one_line_address(self):
		from ai_saas.saas.provisioning import _structured_address

		# A sales user typed everything into address_line1: structure it.
		addr = frappe._dict(address_line1="Av. Eduardo Mondlane, 500, Bairro Polana, Maputo", address_line2="", city="", state="")
		self.assertEqual(_structured_address(addr, "CON-NONE"), {
			"address_line1": "Av. Eduardo Mondlane, 500, Bairro Polana, Maputo",  # the record's own line is kept...
			"neighborhood_or_district": "Bairro Polana", "city": "Maputo", "province": "Maputo Cidade",  # ...gaps parsed
		})
		# Structured parts already on the Address always win.
		addr = frappe._dict(address_line1="Rua 1", address_line2="Bairro X", city="Beira", state="Sofala")
		self.assertEqual(_structured_address(addr, "CON-NONE"),
		                 {"address_line1": "Rua 1", "neighborhood_or_district": "Bairro X", "city": "Beira", "province": "Sofala"})
		# No Address at all: nothing to parse, nothing invented.
		self.assertEqual(_structured_address(None, "CON-NONE"), {})
