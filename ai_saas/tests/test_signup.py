"""Tests for row 7 (A1-A7): the signup API end to end, provisioning patched.
Every document A4 creates is deleted in tearDown — doc.submit() commits."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, cint, getdate, nowdate

from ai_saas.api import signup
from ai_saas.install import TRIAL_CUSTOMER_GROUP

EMAIL = "signup-teste@example.com"
PLAN = "Premium Mensal - MozEconomia Cloud"
SLUG = "a4-teste-empresa"
COMPANY = "_Test Empresa Signup A4"


class TestSignup(FrappeTestCase):
	def setUp(self):
		from ai_saas.tests.helpers import ensure_test_plan
		ensure_test_plan()
		self.industry = frappe.db.get_value("Segment Intelligence Map", {}, "name")
		from ai_saas.install import ensure_trial_customer_group
		ensure_trial_customer_group()
		self._cleanup()

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		for c in frappe.get_all("Contract", {"party_name": ("like", COMPANY + "%")}, pluck="name"):
			d = frappe.get_doc("Contract", c)
			if d.docstatus == 1:
				d.cancel()
			frappe.delete_doc("Contract", c, force=True, ignore_permissions=True)
		for p in frappe.get_all("MZ Tenant Provisioning", {"tenant_slug": SLUG}, pluck="name"):
			frappe.delete_doc("MZ Tenant Provisioning", p, force=True, ignore_permissions=True)
		for cust in frappe.get_all("Customer", {"customer_name": ("like", COMPANY + "%")}, pluck="name"):
			for dt in ("Contact", "Address"):
				for n in frappe.get_all("Dynamic Link", {"link_doctype": "Customer", "link_name": cust, "parenttype": dt}, pluck="parent"):
					frappe.delete_doc(dt, n, force=True, ignore_missing=True, ignore_permissions=True)
			frappe.delete_doc("Customer", cust, force=True, ignore_permissions=True)
		for o in frappe.get_all("Opportunity", {"contact_email": EMAIL}, pluck="name"):
			for t in frappe.get_all("ToDo", {"reference_type": "Opportunity", "reference_name": o}, pluck="name"):
				frappe.delete_doc("ToDo", t, force=True, ignore_permissions=True)
			frappe.delete_doc("Opportunity", o, force=True, ignore_permissions=True)
		for l in frappe.get_all("Lead", {"email_id": EMAIL}, pluck="name"):
			frappe.delete_doc("Lead", l, force=True, ignore_permissions=True)
		for s in frappe.get_all("MZ Signup", {"email": EMAIL}, pluck="name"):
			frappe.delete_doc("MZ Signup", s, force=True, ignore_permissions=True)
		frappe.db.commit()

	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_direct_sale_from_the_desk(self, provision):
		"""Sales fills the same MZ Signup and clicks Criar Conta: same pipeline, custom
		trial window, mz_direct on the Contract, ledger born at Account Created — and
		no nurture email, because the Opportunity never sits at Form Started."""
		provision.side_effect = self._fake_provision()
		# The person once started a web signup: an Opportunity sits at Form Started.
		# The desk sale must reuse it silently — never resend the "finish your
		# registration" welcome to a venda-directa customer.
		lead = frappe.get_doc({"doctype": "Lead", "first_name": "Vendedor Directo",
		                       "email_id": EMAIL, "status": "Lead"}).insert(ignore_permissions=True)
		frappe.get_doc({
			"doctype": "Opportunity", "opportunity_from": "Lead", "party_name": lead.name,
			"company": frappe.db.get_single_value("Global Defaults", "default_company"),
			"sales_stage": "Cloud - Form Started", "contact_email": EMAIL,
		}).insert(ignore_permissions=True)
		doc = frappe.get_doc({
			"doctype": "MZ Signup", "status": "Started", "current_step": 3,
			"full_name": "Vendedor Directo", "email": EMAIL, "phone": "+258 84 000 0002",
			"plan": PLAN, "company_name": COMPANY, "tax_id": "400 76-5432", "industry": self.industry,
			"address": "Av. do Trabalho, 123, Bairro Central, Maputo",
			"subdomain": SLUG, "venda_directa": 1, "trial_days": 45,
		}).insert(ignore_permissions=True)
		queued = frappe.db.count("Email Queue")
		with patch("ai_saas.api.signup._alert_ops"):
			result = signup.create_account_from_desk(doc.name)
		doc.reload()
		self.assertEqual(doc.status, "Provisioning")
		self.assertEqual((doc.tax_id, doc.city), ("400765432", "Maputo"))  # web-form normalisation ran
		contract = frappe.get_doc("Contract", result["contract"])
		self.assertEqual(cint(contract.mz_direct), 1)
		self.assertEqual(getdate(contract.start_date), getdate(add_days(nowdate(), 45)))
		opp = frappe.get_doc("Opportunity", result["opportunity"])
		self.assertEqual((opp.sales_stage, opp.status), ("Cloud - Account Created", "Open"))
		self.assertEqual(frappe.db.count("Email Queue"), queued)  # no Dia 0 resend either
		self.assertEqual(frappe.db.count("Opportunity", {"contact_email": EMAIL}), 1)  # reused, not duplicated
		# Criar Conta works exactly once: the signup left "Started", so a second click
		# is refused and nothing else is created.
		contracts = frappe.db.count("Contract", {"party_name": doc.customer})
		self.assertRaisesRegex(frappe.ValidationError, "só um registo em curso",
		                       signup.create_account_from_desk, doc.name)
		self.assertEqual(frappe.db.count("Contract", {"party_name": doc.customer}), contracts)

	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_direct_sale_failure_marks_failed_and_may_retry(self, provision):
		"""A crash mid-creation must not leave the signup clickable as if nothing happened:
		status → Failed (audit trail in `error`), and only a Failed run WITHOUT a contract
		may be retried — the web path's own rule."""
		provision.side_effect = self._fake_provision()
		doc = frappe.get_doc({
			"doctype": "MZ Signup", "status": "Started", "current_step": 3,
			"full_name": "Vendedor Directo", "email": EMAIL, "phone": "+258 84 000 0002",
			"plan": PLAN, "company_name": COMPANY, "tax_id": "400765432", "industry": self.industry,
			"address": "Av. do Trabalho, 123, Bairro Central, Maputo",
			"subdomain": SLUG, "venda_directa": 1,
		}).insert(ignore_permissions=True)
		with patch("ai_saas.saas.accounts._create_documents", side_effect=frappe.ValidationError("boom")):
			self.assertRaises(frappe.ValidationError, signup.create_account_from_desk, doc.name)
		doc.reload()
		self.assertEqual(doc.status, "Failed")
		self.assertIn("boom", doc.error or "")
		with patch("ai_saas.api.signup._alert_ops"):
			result = signup.create_account_from_desk(doc.name)  # retry allowed: no contract yet
		self.assertTrue(result["contract"])

	def test_direct_sale_rejects_guests_and_missing_fields(self):
		doc = frappe.get_doc({
			"doctype": "MZ Signup", "status": "Started", "full_name": "Vendedor Directo",
			"email": EMAIL, "plan": PLAN,
		}).insert(ignore_permissions=True)
		self.assertRaisesRegex(frappe.ValidationError, "Preencha", signup.create_account_from_desk, doc.name)
		# frappe.only_for is bypassed in tests; what must hold is that the method is
		# whitelisted for logged-in users only — never a guest endpoint.
		self.assertIn(signup.create_account_from_desk, frappe.whitelisted)
		self.assertNotIn(signup.create_account_from_desk, frappe.guest_methods)

	@staticmethod
	def _fake_provision(status="Queued"):
		"""What provision_tenant does that the API depends on: one record per contract."""
		def _side_effect(contract_name):
			frappe.get_doc({
				"doctype": "MZ Tenant Provisioning", "contract": contract_name, "tenant_slug": SLUG,
				"site_name": f"{SLUG}.erp.mozeconomia.co.mz", "status": status,
			}).insert(ignore_permissions=True)
		return _side_effect

	def _walk_to_step3(self, domain=None):
		r = signup._start("Teste Signup", EMAIL, "+258841234567", PLAN, domain)
		token = r["token"]
		signup._update(token, 2, {"company_name": COMPANY, "tax_id": "400123456", "tax_regime": "Normal (16%)",
		                          "industry": self.industry, "address": "Av. 25 de Setembro, 1234, Bairro Central, Maputo"})
		signup._update(token, 3, {"subdomain": SLUG, "plan": PLAN, "terms_accepted": 1})
		return token

	# ---- A1/A2 ------------------------------------------------------------------

	def test_step_one_enters_the_crm(self):
		"""Step 1 is already a Lead and an Opportunity at Form Started — what Sales sees
		and what the nurture reads. A restart for the same address re-uses them."""
		r = signup._start("Teste Signup", EMAIL, "+258841234567", PLAN)
		doc = frappe.get_doc("MZ Signup", {"resume_token": r["token"]})
		self.assertTrue(doc.lead and doc.opportunity)
		opp = frappe.db.get_value("Opportunity", doc.opportunity,
			["sales_stage", "status", "contact_email", "mz_signup", "mz_stage_since", "party_name"], as_dict=True)
		self.assertEqual((opp.sales_stage, opp.status, opp.contact_email, opp.mz_signup, opp.party_name),
		                 ("Cloud - Form Started", "Open", EMAIL, doc.name, doc.lead))
		self.assertTrue(opp.mz_stage_since)

		# step 2 teaches the Lead what the form learnt and restarts the clock
		signup._update(r["token"], 2, {"company_name": COMPANY, "tax_id": "400123456", "tax_regime": "Normal (16%)",
		                               "industry": self.industry, "address": "Av. 25 de Setembro, 1234, Maputo"})
		lead = frappe.db.get_value("Lead", doc.lead, ["company_name", "mz_segment", "city"], as_dict=True)
		self.assertEqual((lead.company_name, lead.mz_segment, lead.city), (COMPANY, self.industry, "Maputo"))

		# a second start for the same address: same Lead, same Opportunity, pointing at the new signup
		r2 = signup._start("Teste Signup", EMAIL, "+258841234567", PLAN)
		doc2 = frappe.get_doc("MZ Signup", {"resume_token": r2["token"]})
		self.assertEqual((doc2.lead, doc2.opportunity), (doc.lead, doc.opportunity))
		self.assertEqual(frappe.db.get_value("Opportunity", doc.opportunity, "mz_signup"), doc2.name)
		self.assertEqual(frappe.db.count("Opportunity", {"party_name": doc.lead}), 1)

	def test_second_start_opens_a_fresh_record_and_never_reveals_the_first(self):
		"""The form never sends anyone to their inbox: a second start continues in the
		browser. What it must not do is hand over the first record — its fields are
		echoed on resume — so it opens a new one and retires the old."""
		r1 = signup._start("Teste Signup", EMAIL, "+258841234567", PLAN)
		self.assertEqual((r1["state"], r1["step"]), ("continue", 2))
		first = frappe.get_doc("MZ Signup", {"resume_token": r1["token"]})
		self.assertEqual((first.status, first.email), ("Started", EMAIL))
		signup._update(r1["token"], 2, {"company_name": COMPANY, "tax_id": "400123456", "tax_regime": "Normal (16%)",
		                                "industry": self.industry, "address": "Av. 25 de Setembro, 1234, Bairro Central, Maputo"})

		r2 = signup._start("Outra Pessoa", EMAIL.upper(), "+258000000000", PLAN)
		self.assertEqual((r2["state"], r2["step"]), ("continue", 2))      # the browser always continues
		self.assertNotEqual(r2["token"], r1["token"])                     # but never with the old token
		second = frappe.get_doc("MZ Signup", {"resume_token": r2["token"]})
		self.assertEqual((second.full_name, second.company_name, second.tax_id), ("Outra Pessoa", None, None))
		first.reload()
		self.assertEqual(first.status, "Superseded")                      # one live signup per address
		self.assertEqual(first.company_name, COMPANY)                     # history kept, not deleted
		self.assertEqual(frappe.db.count("MZ Signup", {"email": EMAIL, "status": "Started"}), 1)

	def test_superseded_signup_revives_when_its_own_browser_continues(self):
		"""The tab that still holds the token is the owner: it picks up where it was."""
		r1 = signup._start("Teste Signup", EMAIL, "+258841234567", PLAN)
		r2 = signup._start("Segunda Tentativa", EMAIL, "", PLAN)
		self.assertEqual(frappe.db.get_value("MZ Signup", {"resume_token": r1["token"]}, "status"), "Superseded")

		st = signup._status(r1["token"])
		self.assertEqual(st["state"], "continue")
		self.assertEqual(st["fields"]["full_name"], "Teste Signup")       # its own data still comes back
		out = signup._update(r1["token"], 2, {"company_name": COMPANY, "tax_id": "400123456", "tax_regime": "Normal (16%)",
		                                      "industry": self.industry, "address": "Av. 25 de Setembro, 1234, Bairro Central, Maputo"})
		self.assertEqual(out["state"], "continue")
		self.assertEqual(frappe.db.get_value("MZ Signup", {"resume_token": r1["token"]}, "status"), "Started")
		self.assertEqual(frappe.db.get_value("MZ Signup", {"resume_token": r2["token"]}, "status"), "Superseded")

	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_email_with_an_account_may_register_another_company(self, provision):
		"""One person, two companies. The inbox is told, the team is told, the door stays
		open — the NUIT is what decides whether an account already exists."""
		provision.side_effect = self._fake_provision()
		done = frappe.get_doc({"doctype": "MZ Signup", "email": EMAIL, "full_name": "Antigo",
		                       "tax_id": "400999999", "status": "Complete"})
		done.insert(ignore_permissions=True)
		with patch("ai_saas.api.signup._send_already_registered_email") as mail, patch("ai_saas.api.signup._alert_ops"):
			r = signup._start("Teste Signup", EMAIL, "", PLAN)
		mail.assert_called_once()                                        # the inbox hears about it...
		self.assertEqual((r["state"], r["step"]), ("continue", 2))       # ...the browser carries on
		self.assertNotIn("duplicate_of_account", signup._status(r["token"]).get("fields", {}))
		signup._update(r["token"], 2, {"company_name": COMPANY, "tax_id": "400123456", "tax_regime": "Normal (16%)",
		                               "industry": self.industry, "address": "Av. 25 de Setembro, 1234, Bairro Central, Maputo"})
		signup._update(r["token"], 3, {"subdomain": SLUG, "plan": PLAN, "terms_accepted": 1})
		with patch("ai_saas.api.signup._alert_ops") as alert:
			out = signup._submit(r["token"])
		self.assertEqual(out["state"], "progress")                       # a second company is allowed
		alert.assert_called_once()                                       # sales is told all the same
		self.assertEqual(frappe.db.get_value("MZ Signup", {"resume_token": r["token"]}, "status"), "Provisioning")

	def test_same_company_twice_is_refused_even_from_a_different_email(self):
		done = frappe.get_doc({"doctype": "MZ Signup", "email": "outra-pessoa@example.com", "full_name": "Antigo",
		                       "tax_id": "400123456", "status": "Complete"})
		done.insert(ignore_permissions=True)
		try:
			token = self._walk_to_step3()
			with patch("ai_saas.api.signup._alert_ops"):
				with self.assertRaises(frappe.ValidationError):
					signup._submit(token)
			self.assertEqual(frappe.db.get_value("MZ Signup", {"resume_token": token}, "status"), "Duplicate")
		finally:
			frappe.delete_doc("MZ Signup", done.name, force=True, ignore_permissions=True)
			frappe.db.commit()

	def test_address_without_a_city_asks_once_instead_of_failing(self):
		"""Address needs a city; a one-line address may not name one. The step asks, keeps
		what was typed, and never asks again unless the address itself changes."""
		r = signup._start("Teste Signup", EMAIL, "", PLAN)
		step2 = {"company_name": COMPANY, "tax_id": "400123456", "tax_regime": "Normal (16%)",
		         "industry": self.industry, "address": "Av. 25 de Setembro"}
		out = signup._update(r["token"], 2, dict(step2))
		self.assertEqual(out["state"], "need_city")                  # asked, not thrown
		self.assertIn("Maputo", out["cities"])
		doc = frappe.get_doc("MZ Signup", {"resume_token": r["token"]})
		self.assertEqual((doc.address, doc.current_step, doc.city or ""), ("Av. 25 de Setembro", 2, ""))

		out = signup._update(r["token"], 2, dict(step2, city="cidade de maputo"))
		self.assertEqual((out["state"], out["step"]), ("continue", 3))
		self.assertEqual(frappe.db.get_value("MZ Signup", doc.name, "city"), "Maputo")   # canonical

		# Back to step 2 and forward again without touching the address: not asked twice.
		self.assertEqual(signup._update(r["token"], 2, dict(step2))["state"], "continue")
		self.assertEqual(frappe.db.get_value("MZ Signup", doc.name, "city"), "Maputo")
		# A new address that does name a city replaces it.
		signup._update(r["token"], 2, dict(step2, address="Rua 3, Chimoio"))
		self.assertEqual(frappe.db.get_value("MZ Signup", doc.name, "city"), "Chimoio")

	def test_billing_address_is_built_from_the_answered_city(self):
		token = self._walk_to_step3()
		frappe.db.set_value("MZ Signup", {"resume_token": token}, {"address": "Av. 25 de Setembro", "city": "Maputo"})
		with patch("ai_saas.saas.provisioning.provision_tenant", side_effect=self._fake_provision()), \
				patch("ai_saas.api.signup._alert_ops"):
			signup._submit(token)
		customer = frappe.db.get_value("MZ Signup", {"resume_token": token}, "customer")
		addr = frappe.db.get_value(
			"Dynamic Link", {"link_doctype": "Customer", "link_name": customer, "parenttype": "Address"}, "parent"
		)
		self.assertTrue(addr, "the Billing Address must exist")
		self.assertEqual(frappe.db.get_value("Address", addr, ["address_line1", "city", "state"]),
		                 ("Av. 25 de Setembro", "Maputo", "Maputo Cidade"))

	def test_update_validates_and_advances(self):
		r = signup._start("Teste Signup", EMAIL, "", PLAN)
		with self.assertRaises(frappe.ValidationError):
			signup._update(r["token"], 2, {"company_name": COMPANY, "tax_id": "12"})
		with self.assertRaises(frappe.PermissionError):
			signup._update("0000", 2, {"company_name": COMPANY})
		r2 = signup._update(r["token"], 2, {"company_name": COMPANY, "tax_id": "400-123-456", "tax_regime": "Normal (16%)",
		                                     "industry": self.industry, "address": "Av. 25 de Setembro, 1234, Bairro Central, Maputo"})
		self.assertEqual(r2["step"], 3)
		self.assertEqual(frappe.db.get_value("MZ Signup", {"resume_token": r["token"]}, "tax_id"), "400123456")
		st = signup._status(r["token"])
		self.assertEqual(st["fields"]["company_name"], COMPANY)  # token holders get their values back

	def test_slug_from_company_is_typeable(self):
		self.assertEqual(signup.slug_from_company("Farmácia Central, Lda"), "farmacia-central")
		self.assertEqual(signup.slug_from_company("João & Filhos Comércio Geral Limitada"), "joao-filhos-comercio")
		self.assertEqual(signup.slug_from_company("Boa Construtora, S.A."), "boa-construtora")
		self.assertEqual(signup.slug_from_company("Sociedade Unipessoal de Transportes do Norte"), "transportes-norte")
		self.assertEqual(signup.slug_from_company("Lda"), "")
		self.assertEqual(signup._suggest_subdomain("Lda"), {"subdomain": ""})
		first = signup._suggest_subdomain(COMPANY)["subdomain"]
		self.assertTrue(signup._check_subdomain(first)["available"])

	def test_check_subdomain(self):
		self.assertFalse(signup._check_subdomain("ab")["available"])
		self.assertFalse(signup._check_subdomain("admin")["available"])
		self.assertTrue(signup._check_subdomain(SLUG)["available"])
		taken = frappe.db.get_value("Contract", {"mz_tenant": ("!=", ""), "docstatus": 1}, "mz_tenant")
		if taken:
			self.assertFalse(signup._check_subdomain(taken)["available"])

	# ---- A3/A4 ------------------------------------------------------------------

	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_submit_creates_everything_and_provisions(self, provision):
		provision.side_effect = self._fake_provision()
		token = self._walk_to_step3()
		# The endpoint is guest-whitelisted: everything _submit creates must succeed as
		# Guest, who has no role on Customer/Contact/Address/Contract (2026-08-28: the
		# Address display rendering ran a permission check and failed in production).
		frappe.set_user("Guest")
		try:
			r = signup._submit(token)
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(r["state"], "progress")
		self.assertIn(SLUG, r["site_url"])

		doc = frappe.get_doc("MZ Signup", {"resume_token": token})
		self.assertEqual(doc.status, "Provisioning")
		self.assertTrue(doc.lead and doc.customer and doc.contract and doc.provisioning)
		provision.assert_called_once_with(doc.contract)

		c = frappe.db.get_value("Contract", doc.contract,
			["docstatus", "is_signed", "status", "mz_tenant", "mz_subscription_plan", "contact_email", "mz_contact_mobile", "contract_terms"], as_dict=True)
		self.assertEqual((c.docstatus, c.is_signed, c.status), (1, 0, "Unsigned"))
		# contact fields are fetched from the Customer's primary contact, never copied
		self.assertEqual((c.mz_tenant, c.mz_subscription_plan, c.contact_email, c.mz_contact_mobile), (SLUG, PLAN, EMAIL, "+258841234567"))
		self.assertIn(COMPANY, c.contract_terms)
		self.assertNotIn("{{", c.contract_terms)

		cust = frappe.db.get_value("Customer", doc.customer, ["customer_group", "tax_id", "email_id", "customer_primary_contact", "lead_name"], as_dict=True)
		self.assertEqual(cust.customer_group, TRIAL_CUSTOMER_GROUP)
		self.assertEqual(cust.tax_id, "400123456")
		self.assertEqual(cust.email_id, EMAIL)
		self.assertTrue(cust.customer_primary_contact)
		self.assertEqual(cust.lead_name, doc.lead)
		self.assertEqual(frappe.db.get_value("Lead", doc.lead, "mz_segment"), self.industry)
		# the sector reaches the Contract and decides the apps (base + segment's, bench-filtered)
		from ai_saas.saas.provisioning import apps_for_segment
		con = frappe.get_doc("Contract", doc.contract)
		self.assertEqual(con.mz_segment, self.industry)
		self.assertEqual([r.app_name for r in con.mz_apps_to_install], apps_for_segment(self.industry, con.mz_subscription_plan))
		self.assertEqual((con.mz_domain, con.mz_tenant_url), (".erp.mozeconomia.co.mz", SLUG + ".erp.mozeconomia.co.mz"))
		self.assertEqual([r.app_name for r in con.mz_apps_to_install][:2] if not con.mz_apps_to_install[0].app_name == "hrms" else [r.app_name for r in con.mz_apps_to_install][1:3], ["erpnext", "erpnext_mz"])
		opp = frappe.db.get_value("Opportunity", doc.opportunity, ["sales_stage", "opportunity_from", "contact_person", "mz_signup"], as_dict=True)
		self.assertEqual((opp.sales_stage, opp.opportunity_from, opp.mz_signup), ("Cloud - Account Created", "Lead", doc.name))
		self.assertEqual(opp.contact_person, cust.customer_primary_contact)
		self.assertEqual(frappe.db.count("Opportunity", {"party_name": doc.lead}), 1)  # step 1's record, advanced — not a second one
		addr = frappe.db.get_value("Address", {"address_title": COMPANY}, ["address_type", "address_line1", "address_line2", "city", "state"], as_dict=True)
		self.assertEqual((addr.address_type, addr.address_line1, addr.address_line2, addr.city, addr.state),
		                 ("Billing", "Av. 25 de Setembro, 1234", "Bairro Central", "Maputo", "Maputo Cidade"))
		self.assertEqual(frappe.db.get_value("MZ Signup", {"resume_token": token}, "city"), "Maputo")

		# submitting again is a no-op (state only)
		self.assertEqual(signup._submit(token)["state"], "progress")

	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_partner_form_fixes_domain_sector_and_apps(self, provision):
		"""/registo-curati (2026-08-29): the domain is the form's, the sector is fixed to the
		domain's profile (whatever the browser sends), and the pharmacy apps ride on the Contract."""
		provision.side_effect = self._fake_provision()
		token = self._walk_to_step3(".erp.curati.co.mz")
		doc = frappe.get_doc("MZ Signup", {"resume_token": token})
		self.assertEqual((doc.mz_domain, doc.industry), (".erp.curati.co.mz", "Saúde & Bem-Estar"))
		self.assertTrue(doc.resume_url.endswith(f"/registo-curati?token={token}"))
		r = signup._submit(token)
		self.assertEqual(r["site_url"], f"https://{SLUG}.erp.curati.co.mz")
		doc.reload()
		con = frappe.get_doc("Contract", doc.contract)
		self.assertEqual((con.mz_domain, con.mz_tenant_url, con.mz_segment), (".erp.curati.co.mz", SLUG + ".erp.curati.co.mz", "Saúde & Bem-Estar"))
		apps = [row.app_name for row in con.mz_apps_to_install]
		self.assertLess(apps.index("healthcare"), apps.index("curati_connect"))
		self.assertIn("pos_next", apps)
		self.assertEqual(frappe.db.get_value("Lead", doc.lead, "mz_segment"), "Saúde & Bem-Estar")

	def test_unknown_domain_falls_back_to_the_default(self):
		token = self._walk_to_step3("evil.com")
		doc = frappe.get_doc("MZ Signup", {"resume_token": token})
		self.assertEqual((doc.mz_domain, doc.industry), (".erp.mozeconomia.co.mz", self.industry))
		self.assertTrue(doc.resume_url.endswith(f"/registo?token={token}"))

	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_duplicate_nuit_is_refused_generically(self, provision):
		"""The company already has a cloud account — that, and only that, is the duplicate."""
		token = self._walk_to_step3()
		twin = frappe.get_doc({"doctype": "Customer", "customer_name": COMPANY + " Existente", "customer_type": "Company",
		                       "customer_group": TRIAL_CUSTOMER_GROUP, "territory": signup._default_territory(),
		                       "tax_id": "400123456"}).insert(ignore_permissions=True)
		contract = frappe.get_doc({"doctype": "Contract", "party_type": "Customer", "party_name": twin.name,
		                           "start_date": frappe.utils.nowdate(), "contract_terms": "x",
		                           "mz_tenant": "outro-site"}).insert(ignore_permissions=True)
		frappe.db.set_value("Contract", contract.name, "docstatus", 1)   # submitted, without the lifecycle hooks
		try:
			with patch("ai_saas.api.signup._alert_ops") as alert:
				with self.assertRaises(frappe.ValidationError) as ctx:
					signup._submit(token)
			self.assertIn("Não foi possível concluir", str(ctx.exception))
			alert.assert_called_once()
			self.assertEqual(frappe.db.get_value("MZ Signup", {"resume_token": token}, "status"), "Duplicate")
			self.assertFalse(frappe.db.exists("Contract", {"mz_tenant": SLUG}))
			provision.assert_not_called()
		finally:
			frappe.db.set_value("Contract", contract.name, "docstatus", 0)
			frappe.delete_doc("Contract", contract.name, force=True, ignore_permissions=True)
			frappe.db.commit()

	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_customer_leaves_with_its_primary_contact_and_address(self, provision):
		"""ERPNext addresses a customer through these two fields: email and print formats
		both dead-end without them."""
		provision.side_effect = self._fake_provision()
		token = self._walk_to_step3()
		with patch("ai_saas.api.signup._alert_ops"):
			signup._submit(token)
		doc = frappe.get_doc("MZ Signup", {"resume_token": token})
		customer = frappe.get_doc("Customer", doc.customer)

		self.assertTrue(customer.customer_primary_contact)
		self.assertTrue(customer.customer_primary_address)
		self.assertEqual(customer.email_id, EMAIL)
		self.assertEqual(customer.mobile_no, "+258841234567")
		self.assertIn("Maputo", customer.primary_address)                 # the display ERPNext prints

		contact = frappe.get_doc("Contact", customer.customer_primary_contact)
		self.assertEqual(contact.is_primary_contact, 1)
		self.assertEqual(contact.email_id, EMAIL)
		self.assertEqual(contact.mobile_no, "+258841234567")              # SMS needs the primary mobile
		# ERPNext links the Lead's contact to the new Customer (Customer.link_address_and_contact),
		# so the funnel reuses that one rather than making a second: both links are expected.
		self.assertIn(customer.name, [l.link_name for l in contact.links])

		address = frappe.get_doc("Address", customer.customer_primary_address)
		self.assertEqual((address.address_type, address.is_primary_address, address.is_shipping_address),
		                 ("Billing", 1, 1))
		self.assertEqual((address.city, address.state), ("Maputo", "Maputo Cidade"))
		self.assertIn(customer.name, [l.link_name for l in address.links])

	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_an_existing_customer_of_the_house_is_reused_not_refused(self, provision):
		"""MozEconomia's own ERP holds every customer it has — on-prem, consulting, POS.
		One of them buying the cloud product is the same customer, not a duplicate."""
		provision.side_effect = self._fake_provision()
		known = frappe.get_doc({"doctype": "Customer", "customer_name": COMPANY + " Existente", "customer_type": "Company",
		                        "customer_group": "Comercial" if frappe.db.exists("Customer Group", "Comercial") else TRIAL_CUSTOMER_GROUP,
		                        "territory": signup._default_territory(), "tax_id": "400123456",
		                        "email_id": "financeiro@example.com"}).insert(ignore_permissions=True)
		group_before = known.customer_group
		token = self._walk_to_step3()
		with patch("ai_saas.api.signup._alert_ops"):
			out = signup._submit(token)
		self.assertEqual(out["state"], "progress")                       # not refused
		doc = frappe.get_doc("MZ Signup", {"resume_token": token})
		self.assertEqual(doc.customer, known.name)                       # reused, no second Customer
		self.assertEqual(frappe.db.count("Customer", {"tax_id": "400123456"}), 1)
		known.reload()
		self.assertEqual(known.customer_group, group_before)             # its commercial group is left alone
		self.assertEqual(known.email_id, "financeiro@example.com")       # and so is what sales filled in
		self.assertTrue(known.customer_primary_contact)                  # but the missing chain is completed

	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_ceiling_refuses_and_keeps_started(self, provision):
		token = self._walk_to_step3()
		real = signup.get_settings()
		real.max_signups_per_day = 0
		with patch("ai_saas.api.signup.get_settings", return_value=real), patch("ai_saas.api.signup._alert_ops"):
			with self.assertRaises(frappe.ValidationError):
				signup._submit(token)
		self.assertEqual(frappe.db.get_value("MZ Signup", {"resume_token": token}, "status"), "Started")
		provision.assert_not_called()

	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_submit_without_provisioning_record_fails_loudly(self, provision):
		token = self._walk_to_step3()
		with patch("ai_saas.saas.accounts._alert_ops") as alert:
			r = signup._submit(token)
		self.assertEqual(r["state"], "failed")
		alert.assert_called_once()
		doc = frappe.get_doc("MZ Signup", {"resume_token": token})
		self.assertTrue(doc.contract)                    # documents exist and are linked...
		self.assertFalse(doc.provisioning)               # ...but nothing is provisioning
		# and the signup cannot be re-submitted (the team retries via C1)
		self.assertEqual(signup._submit(token)["state"], "failed")

	def test_email_change_is_validated_and_supersedes_the_other_live_signup(self):
		r = signup._start("A", EMAIL, "", PLAN)
		other = frappe.get_doc({"doctype": "MZ Signup", "email": "outro@example.com", "full_name": "B", "status": "Started"}).insert(ignore_permissions=True)
		try:
			with self.assertRaises(frappe.ValidationError):
				signup._update(r["token"], 1, {"email": "não é email"})
			self.assertEqual(frappe.db.get_value("MZ Signup", {"resume_token": r["token"]}, "email"), EMAIL)
			# Correcting the email to one that already has a signup in progress is allowed —
			# nothing of that record is shown or reused; it simply stops being the live one.
			signup._update(r["token"], 1, {"full_name": "A", "email": "OUTRO@example.com"})
			self.assertEqual(frappe.db.get_value("MZ Signup", {"resume_token": r["token"]}, "email"), "outro@example.com")
			self.assertEqual(frappe.db.get_value("MZ Signup", other.name, "status"), "Superseded")
		finally:
			frappe.delete_doc("MZ Signup", other.name, force=True, ignore_permissions=True)

	# ---- A6 ---------------------------------------------------------------------

	@patch("ai_saas.saas.provisioning.provision_tenant")
	def test_status_reconciles_from_provisioning_record(self, provision):
		provision.side_effect = self._fake_provision(status="Failed")
		token = self._walk_to_step3()
		signup._submit(token)
		doc = frappe.get_doc("MZ Signup", {"resume_token": token})
		prov = frappe.get_doc("MZ Tenant Provisioning", doc.provisioning)
		self.assertEqual(signup._status(token)["state"], "failed")
		# A C1 retry that succeeds must turn a Failed signup Complete.
		prov.db_set("status", "Active")
		st = signup._status(token)
		self.assertEqual(st["state"], "complete")
		self.assertIn(SLUG, st["site_url"])
