"""Tests for row 6: C2 (delivery email from an Email Template), D3 (trial
countdown replaces the nurture set), D4 (Calendly booking link from settings)."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, nowdate

from ai_saas.install import WELCOME_EMAIL_TEMPLATE
from ai_saas.saas import provisioning
from ai_saas.tests.helpers import FunnelTestCase

TEST_CUSTOMER = "_Test Cliente AI SaaS M"
TEST_PLAN = "Premium Mensal - MozEconomia Cloud"
TEST_SLUG = "c2-teste"


class TestMessaging(FunnelTestCase):
	CUSTOMER = TEST_CUSTOMER
	CUSTOMER_EMAIL = "m@example.com"  # the Contract's contact fields fetch from the Customer

	def setUp(self):
		super().setUp()
		self.contract = self.make_contract(submit=True, slug=TEST_SLUG, contract_terms="Termos M.")
		self.prov = self.make_prov(self.contract.name, customer_name=TEST_CUSTOMER, contact_email="m@example.com")
		frappe.db.commit()

	# ---- C2 ---------------------------------------------------------------------

	def test_delivery_email_renders_trial_copy_with_both_links(self):
		self.assertTrue(frappe.db.exists("Email Template", WELCOME_EMAIL_TEMPLATE))
		email = provisioning._render_welcome_email(self.prov, "https://x/update-password?key=abc")
		self.assertIn("/activar?contract=" + self.contract.name, email["message"])
		self.assertIn(provisioning.get_booking_url(), email["message"])
		self.assertNotIn("/book_appointment", email["message"])
		self.assertIn("key=abc", email["message"])
		self.assertIn(frappe.utils.formatdate(self.contract.start_date), email["message"])
		self.assertNotIn("7 dias", email["message"])          # no billing copy on a trial
		self.assertNotIn("{{", email["message"])
		self.assertIn("pronta", email["subject"])

	def test_delivery_email_renders_billing_copy_when_signed(self):
		frappe.db.set_value("Contract", self.contract.name, "is_signed", 1)
		email = provisioning._render_welcome_email(self.prov, "https://x/reset")
		self.assertIn("7 dias", email["message"])
		self.assertNotIn("/activar", email["message"])
		self.assertIn("activa", email["subject"])

	def test_send_uses_template(self):
		with patch("frappe.sendmail") as sendmail:
			provisioning._send_welcome_email(self.prov, "https://x/reset")
		kwargs = sendmail.call_args.kwargs
		self.assertEqual(kwargs["recipients"], ["m@example.com"])
		self.assertIn("/activar", kwargs["message"])

	# ---- D3 ---------------------------------------------------------------------

	def test_nurture_set_retargeted_and_countdown_exists(self):
		# G1: the nurture emails read the Opportunity — the funnel's ledger — at Form Started.
		nurture = frappe.get_all(
			"Notification", filters={"name": ("like", "AI SaaS - Lead Nurture%")},
			fields=["name", "document_type", "event", "date_changed", "days_in_advance", "condition"],
		)
		self.assertEqual(sorted(n.days_in_advance for n in nurture), [0, 3, 10, 20])
		for n in nurture:
			self.assertEqual(n.document_type, "Opportunity")
			self.assertIn('doc.sales_stage == "Cloud - Form Started"', n.condition)
			self.assertEqual(n.date_changed, None if n.event == "New" else "mz_stage_since")
		fields = frappe.get_all("Notification Recipient", filters={"parent": ("like", "AI SaaS - Lead Nurture%")}, pluck="receiver_by_document_field")
		self.assertEqual(set(fields), {"contact_email"})
		# G2 / G3: the same shape, off the same anchor, on the stages the lifecycle reports.
		for prefix, stage, days in (("AI SaaS - Trial Expirado%", "Cloud - Trial Expired", [1, 7, 21]),
		                            ("AI SaaS - Conta Encerrada%", "Cloud - Closed", [3, 30])):
			rows = frappe.get_all("Notification", filters={"name": ("like", prefix)},
			                      fields=["document_type", "event", "date_changed", "days_in_advance", "condition"])
			self.assertEqual(sorted(r.days_in_advance for r in rows), days)
			for r in rows:
				self.assertEqual((r.document_type, r.event, r.date_changed), ("Opportunity", "Days After", "mz_stage_since"))
				self.assertIn(f'doc.sales_stage == "{stage}"', r.condition)
		rows = frappe.get_all(
			"Notification", filters={"name": ("like", "AI SaaS - Trial - %")},
			fields=["name", "event", "date_changed", "days_in_advance", "condition", "document_type"],
		)
		self.assertEqual(sorted(r.days_in_advance for r in rows), [0, 1, 2, 3, 7])  # 2 = 'Primeira factura' (Days After creation)
		for r in rows:
			expected = ("Contract", "Days After", "creation") if "Primeira factura" in r.name else ("Contract", "Days Before", "start_date")
			self.assertEqual((r.document_type, r.event, r.date_changed), expected, r.name)
			self.assertNotIn("mz_account_phase", r.condition)  # the phase is derived, never a field
			self.assertIn("not doc.is_signed", r.condition)
		roles = frappe.get_all("Notification Recipient", filters={"parent": ("like", "AI SaaS - Trial - %")}, pluck="receiver_by_role")
		self.assertFalse(any(roles))
		# The Calendly link is read from MZ SaaS Settings, never hardcoded in a message.
		self.assertFalse(frappe.db.sql("select 1 from tabNotification where name like 'AI SaaS%%' and message like '%%calendly%%'"))
		self.assertFalse(frappe.db.sql("select 1 from tabNotification where name like 'AI SaaS%%' and message like '%%book_appointment%%'"))
		self.assertFalse(frappe.db.sql("select 1 from tabNotification where name like 'AI SaaS%%' and message like '%%lead-onboarding%%'"))

	def test_countdown_message_renders_with_activation_link(self):
		from frappe.email.doctype.notification.notification import get_context

		notif = frappe.get_doc("Notification", "AI SaaS - Trial - 7 dias")
		doc = frappe.get_doc("Contract", self.contract.name)
		# Notification.send() renders the message with {"doc", "alert", "comments"} —
		# the Jinja env's own `frappe` (get_all, format_date, hooks methods) applies.
		# get_context() is the CONDITION's namespace: frappe.utils only.
		message = frappe.render_template(notif.message, {"doc": doc, "alert": notif, "comments": None})
		self.assertIn("/activar?contract=" + doc.name, message)
		self.assertIn(frappe.db.get_single_value("MZ SaaS Settings", "booking_url"), message)
		self.assertNotIn("{{", message)
		self.assertTrue(frappe.safe_eval(notif.condition, None, get_context(doc)))
		frappe.db.set_value("Contract", doc.name, "is_signed", 1)
		self.assertFalse(frappe.safe_eval(notif.condition, None, get_context(frappe.get_doc("Contract", doc.name))))

	@staticmethod
	def _opportunity_for(signup):
		"""The (unsaved) Opportunity a signup opens — what the nurture Notifications receive."""
		return frappe.get_doc({
			"doctype": "Opportunity", "opportunity_from": "Lead", "sales_stage": "Cloud - Form Started",
			"contact_email": signup.email, "mz_signup": signup.name, "status": "Open",
		})

	def test_nurture_message_renders_resume_link(self):
		from frappe.email.doctype.notification.notification import get_context

		signup = frappe.get_doc({
			"doctype": "MZ Signup", "full_name": "Teste G1", "email": "g1@example.com",
			"company_name": "Empresa G1", "subdomain": "g1-teste", "current_step": 2,
		}).insert(ignore_permissions=True)
		opp = self._opportunity_for(signup)
		try:
			for name in ("AI SaaS - Lead Nurture - Dia 0", "AI SaaS - Lead Nurture - Dia 20"):
				notif = frappe.get_doc("Notification", name)
				message = frappe.render_template(notif.message, {"doc": opp, "alert": notif, "comments": None})
				self.assertIn("/registo?token=" + signup.resume_token, message)
				self.assertNotIn("{{", message)
				self.assertTrue(frappe.safe_eval(notif.condition, None, get_context(opp)))
			opp.sales_stage = "Cloud - Account Created"  # the form was finished: the sequence stops
			self.assertFalse(frappe.safe_eval(notif.condition, None, get_context(opp)))
		finally:
			frappe.delete_doc("MZ Signup", signup.name, force=True, ignore_permissions=True)

	def test_welcome_message_sells_without_the_credit_card_line(self):
		"""Dia 0 (2026-08-28 language): we have you, here is the way back in, one reason to finish."""
		signup = frappe.get_doc({
			"doctype": "MZ Signup", "full_name": "Teste Boas-Vindas", "email": "welcome@example.com",
			"company_name": "Empresa Boas-Vindas", "current_step": 2,
		}).insert(ignore_permissions=True)
		try:
			notif = frappe.get_doc("Notification", "AI SaaS - Lead Nurture - Dia 0")
			self.assertIn("continue quando quiser", notif.subject)
			message = frappe.render_template(notif.message, {"doc": self._opportunity_for(signup), "alert": notif, "comments": None})
			self.assertIn("Obrigado por começar o registo", message)
			self.assertRegex(message, r"(Bom dia|Boa tarde|Boa noite) Teste,")
			self.assertEqual(message.count("padding:12px 22px"), 1)     # one CTA
			self.assertIn("Empresa Boas-Vindas", message)
			self.assertIn("/registo?token=" + signup.resume_token, message)
			trial_days = frappe.db.get_single_value("MZ SaaS Settings", "trial_length_days") or 14
			self.assertIn(f"{trial_days} dias", message)                    # the offer, from the settings
			self.assertNotIn("cartão de crédito", message)                  # not how Mozambique buys
			self.assertNotIn("{{", message)
		finally:
			frappe.delete_doc("MZ Signup", signup.name, force=True, ignore_permissions=True)

	# ---- billing: the credit note, and the invoice mails that must not fire on one ----

	def _credit_note(self, return_against="ACC-SINV-2026-00001"):
		"""A credit note as ERPNext builds one — negative total — never inserted: these
		tests are about what the notification says and when it fires."""
		return frappe.get_doc({
			"doctype": "Sales Invoice", "customer": TEST_CUSTOMER, "customer_name": TEST_CUSTOMER,
			"is_return": 1, "return_against": return_against, "posting_date": nowdate(),
			"currency": "MZN", "grand_total": -2999.0, "outstanding_amount": -2999.0,
			"contact_email": "m@example.com", "subscription": None, "is_pos": 0,
		})

	def test_credit_note_notification_is_configured_and_renders(self):
		notif = frappe.get_doc("Notification", "AI SaaS - Nota de Crédito")
		self.assertEqual((notif.document_type, notif.event, notif.enabled), ("Sales Invoice", "Submit", 1))
		self.assertEqual(notif.print_format, "Nota de Crédito (MZ)")       # the MZ credit-note layout
		self.assertEqual(notif.attach_print, 1)
		self.assertEqual(notif.recipients[0].receiver_by_document_field, "contact_email")
		self.assertIn("is_billing_contact", notif.recipients[0].cc)        # same fan-out as the invoice

		message = frappe.render_template(notif.message, {"doc": self._credit_note(), "alert": notif, "comments": None})
		self.assertIn("anula a fatura ACC-SINV-2026-00001", message)
		self.assertIn("2.999,00", message)                                 # the value, positive
		self.assertNotIn("-2.999", message)
		self.assertIn("Equipa MozEconomia Cloud", message)                 # the house sign-off
		self.assertNotIn("{{", message)

		# A credit note with nothing to cancel says so instead of naming an invoice.
		free = frappe.render_template(notif.message, {"doc": self._credit_note(return_against=None),
		                                              "alert": notif, "comments": None})
		self.assertIn("crédito a seu favor", free)
		self.assertNotIn("anula a fatura", free)

	def test_invoice_mails_never_fire_on_a_credit_note(self):
		from frappe.email.doctype.notification.notification import get_context

		credit = self._credit_note()
		invoice = frappe.get_doc({
			"doctype": "Sales Invoice", "customer": TEST_CUSTOMER, "customer_name": TEST_CUSTOMER,
			"is_return": 0, "posting_date": nowdate(), "currency": "MZN", "grand_total": 2999.0,
			"outstanding_amount": 2999.0, "subscription": "SUB-TESTE", "contact_email": "m@example.com",
			"contact_mobile": "+258840000000", "is_pos": 0,
		})
		invoice_mails = [n for n in frappe.get_all(
			"Notification", filters={"document_type": "Sales Invoice", "name": ("like", "AI SaaS%")}, pluck="name"
		) if n != "AI SaaS - Nota de Crédito"]
		self.assertGreaterEqual(len(invoice_mails), 7)
		for name in invoice_mails:
			notif = frappe.get_doc("Notification", name)
			self.assertFalse(frappe.safe_eval(notif.condition, None, get_context(credit)), f"{name} fired on a credit note")
			self.assertTrue(frappe.safe_eval(notif.condition, None, get_context(invoice)), f"{name} stopped firing on an invoice")

		credit_notif = frappe.get_doc("Notification", "AI SaaS - Nota de Crédito")
		self.assertTrue(frappe.safe_eval(credit_notif.condition, None, get_context(credit)))
		self.assertFalse(frappe.safe_eval(credit_notif.condition, None, get_context(invoice)))

	# ---- 2026-08-28 cycle review ---------------------------------------------------

	def test_cycle_review_drops_and_gates(self):
		from frappe.email.doctype.notification.notification import get_context
		for gone in ("AI SaaS - Lembrete 1", "AI SaaS - Lead Nurture - Dia 5", "AI SaaS - Lead Nurture - Dia 15",
		             "AI SaaS - Lead Nurture - Dia 30", "AI SaaS - Aviso de Desativação"):
			self.assertFalse(frappe.db.exists("Notification", gone), gone)
		nurture = sorted(frappe.get_all("Notification", {"name": ("like", "AI SaaS - Lead Nurture%")}, pluck="name"))
		self.assertEqual(nurture, [f"AI SaaS - Lead Nurture - Dia {n}" for n in (0, 10, 20, 3)])

		# The invoice SMS is Cloud-only: erp.local invoices every MozEconomia customer.
		sms = frappe.get_doc("Notification", "AI SaaS - SMS Fatura Emitida")
		plain = frappe._dict({"doctype": "Sales Invoice", "subscription": None, "is_return": 0,
		                      "outstanding_amount": 100.0, "contact_mobile": "+258840000000"})
		self.assertFalse(frappe.safe_eval(sms.condition, None, get_context(plain)))
		plain.subscription = "SUB-1"
		self.assertTrue(frappe.safe_eval(sms.condition, None, get_context(plain)))

		# No message body links to a page that does not exist.
		self.assertFalse(frappe.db.sql("select 1 from tabNotification where name like 'AI SaaS%%' and message like '%%cloud-feedback%%'"))

	def test_overdue_rhythm_and_sms_deadlines(self):
		from frappe.email.doctype.notification.notification import get_context
		inv = frappe._dict({"doctype": "Sales Invoice", "name": "ACC-SINV-2026-00001", "subscription": "SUB-1", "is_return": 0,
		                    "outstanding_amount": 2999.0, "currency": "MZN", "contact_mobile": "+258840000000",
		                    "customer_name": "Empresa X", "due_date": frappe.utils.nowdate(), "posting_date": frappe.utils.nowdate()})
		expected = {
			"AI SaaS - Aviso de Suspensão": ("Days After", 1, "due_date"),
			"AI SaaS - Aviso de Atraso 7 dias": ("Days After", 7, "due_date"),
			"AI SaaS - Aviso de Atraso 15 dias": ("Days After", 15, "due_date"),
			"AI SaaS - Aviso de Suspensão em 3 dias": ("Days After", 30, "due_date"),
			"AI SaaS - SMS Vencimento Hoje": ("Days Before", 0, "due_date"),
			"AI SaaS - SMS Atraso 7 dias": ("Days After", 7, "due_date"),
			"AI SaaS - SMS Suspensão em 3 dias": ("Days After", 30, "due_date"),
		}
		for name, (event, days, field) in expected.items():
			n = frappe.get_doc("Notification", name)
			self.assertEqual((n.event, n.days_in_advance, n.date_changed), (event, days, field), name)
			self.assertTrue(frappe.safe_eval(n.condition, None, get_context(inv)), name)
			message = frappe.render_template(n.message, {"doc": inv, "alert": n, "comments": None})
			self.assertNotIn("{{", message)
			if n.channel == "SMS":
				self.assertLess(len(message), 320, name)
				self.assertIn(inv.name, message)
		three = frappe.render_template(frappe.get_doc("Notification", "AI SaaS - Aviso de Suspensão em 3 dias").message, {"doc": inv, "alert": None, "comments": None})
		self.assertNotIn("perda definitiva", three)
		self.assertIn("3 dias", three)

	def test_trial_first_invoice_email_and_sms_today(self):
		from frappe.email.doctype.notification.notification import get_context
		first = frappe.get_doc("Notification", "AI SaaS - Trial - Primeira factura")
		self.assertEqual((first.event, first.days_in_advance, first.date_changed), ("Days After", 2, "creation"))
		sms = frappe.get_doc("Notification", "AI SaaS - SMS Trial Hoje")
		self.assertEqual((sms.channel, sms.event, sms.days_in_advance, sms.date_changed), ("SMS", "Days Before", 0, "start_date"))
		self.assertEqual(sms.recipients[0].receiver_by_document_field, "mz_contact_mobile")
		doc = frappe._dict({"doctype": "Contract", "name": "CON-X", "docstatus": 1, "is_signed": 0,
		                    "contact_email": "c@example.com", "mz_contact_mobile": "+258840000000", "party_name": "Empresa X",
		                    "mz_tenant_url": "x.erp.mozeconomia.co.mz", "start_date": frappe.utils.nowdate()})
		self.assertTrue(frappe.safe_eval(first.condition, None, get_context(doc)))
		self.assertTrue(frappe.safe_eval(sms.condition, None, get_context(doc)))
		doc.mz_contact_mobile = ""
		self.assertFalse(frappe.safe_eval(sms.condition, None, get_context(doc)))

	# ---- D4 (Calendly, not /book_appointment) --------------------------------------

	def test_booking_url_is_a_setting_seeded_on_install(self):
		from ai_saas.install import DEFAULT_BOOKING_URL, ensure_booking_url
		ensure_booking_url()
		self.assertTrue(frappe.db.get_single_value("MZ SaaS Settings", "booking_url"))
		self.assertTrue(DEFAULT_BOOKING_URL.startswith("https://calendly.com/"))
		self.assertFalse(frappe.db.exists("Notification", "AI SaaS - Marcação Confirmada"))



	def test_g3_reaches_an_account_with_no_signup(self):
		"""The resolver is the only account lookup: a legacy/create_account Opportunity
		(no MZ Signup anywhere) passes the condition and renders with the /reactivar link."""
		from ai_saas.saas import crm

		frappe.db.set_value("MZ Tenant Provisioning", self.prov.name, "backup_path", "/tmp/x")
		opp = frappe.get_doc({
			"doctype": "Opportunity", "opportunity_from": "Customer", "party_name": TEST_CUSTOMER,
			"company": frappe.db.get_single_value("Global Defaults", "default_company"),
			"sales_stage": "Cloud - Closed", "contact_email": "m@example.com",
		}).insert(ignore_permissions=True)
		self.track("Opportunity", opp.name)
		self.assertEqual(crm.find_contract(opp.name), self.contract.name)
		record = frappe.get_doc("Notification", "AI SaaS - Conta Encerrada - Dia 3")
		self.assertTrue(frappe.safe_eval(record.condition, None, {"doc": opp}))
		html = frappe.render_template(record.message, {"doc": opp, "alert": None, "comments": None})
		self.assertIn("/reactivar", html)
		self.assertNotIn("{{", html)

	def test_an_empty_rendering_never_sends(self):
		"""The override makes a false body-gate mean no email at all — here: a trial
		countdown for a site that is no longer Active."""
		frappe.db.set_value("MZ Tenant Provisioning", self.prov.name, "status", "Suspended")
		queued = frappe.db.count("Email Queue")
		record = frappe.get_doc("Notification", "AI SaaS - Trial - Hoje")
		self.assertIsInstance(record, __import__("ai_saas.overrides.notification", fromlist=["SilentWhenEmptyNotification"]).SilentWhenEmptyNotification)
		record.send(frappe.get_doc("Contract", self.contract.name))
		self.assertEqual(frappe.db.count("Email Queue"), queued)
		# and the same notification DOES send while the site is Active
		frappe.db.set_value("MZ Tenant Provisioning", self.prov.name, "status", "Active")
		record.send(frappe.get_doc("Contract", self.contract.name))
		self.assertEqual(frappe.db.count("Email Queue"), queued + 1)


class TestCommunicationLanguage(FrappeTestCase):
	"""One voice (docs/communication-copy-review.md): time-of-day greeting to a person,
	account-manager signature, Mozambican spelling, one inbox for relationship emails."""

	BILLING = frozenset({"AI SaaS - Fatura Emitida", "AI SaaS - Nota de Crédito", "AI SaaS - Recibo de Pagamento",
	                     "AI SaaS - SMS Fatura Emitida"})

	def test_greeting_follows_site_hour_and_first_name(self):
		from datetime import datetime

		from ai_saas.utils.jinja import mz_first_name, mz_greeting
		for hour, word in ((7, "Bom dia"), (11, "Bom dia"), (12, "Boa tarde"), (18, "Boa tarde"), (19, "Boa noite"), (23, "Boa noite")):
			with patch("frappe.utils.now_datetime", return_value=datetime(2026, 8, 28, hour, 5)):
				self.assertEqual(mz_greeting("Ana Maria Sitoe"), f"{word} Ana,", hour)
				self.assertEqual(mz_greeting(None), f"{word},", hour)
		self.assertEqual(mz_first_name("  Carlos  Matola "), "Carlos")
		self.assertEqual(mz_first_name(""), "")

	def test_signature_names_the_account_manager_or_falls_back(self):
		from ai_saas.utils.jinja import mz_signature
		sig = mz_signature("Administrator")
		self.assertIn("Com boas energias,", sig)
		self.assertIn(frappe.db.get_value("User", "Administrator", "full_name"), sig)
		self.assertIn("Equipa MozEconomia Cloud", sig)
		with patch("frappe.db.get_single_value", return_value=None):
			self.assertEqual(mz_signature(None), "<p>Com boas energias,<br>Equipa MozEconomia Cloud</p>")

	def test_daily_alerts_pinned_to_eight(self):
		from ai_saas.install import DAILY_ALERTS_CRON, DAILY_ALERTS_JOB, ensure_daily_alerts_hour
		ensure_daily_alerts_hour()
		job = frappe.db.get_value("Scheduled Job Type", {"method": DAILY_ALERTS_JOB}, ["frequency", "cron_format"], as_dict=True)
		self.assertEqual((job.frequency, job.cron_format), ("Cron", DAILY_ALERTS_CRON))

	def test_relationship_messages_speak_to_a_person_in_one_voice(self):
		rows = frappe.get_all("Notification", filters={"name": ("like", "AI SaaS%"), "enabled": 1},
		                      fields=["name", "document_type", "subject", "message", "channel"])
		for r in rows:
			if r.name in self.BILLING:
				continue
			text = (r.subject or "") + (r.message or "")
			self.assertNotRegex(text, r"\b[Ff]atura", r.name)  # Mozambican spelling
			self.assertNotIn("{{ doc.party_name }},", text, r.name)  # never greet the company
			if r.document_type in ("Contract", "MZ Signup") and r.channel == "Email":
				self.assertIn("mz_greeting(", r.message, r.name)
				self.assertNotIn("contacto@", text, r.name)
				self.assertNotIn("<td bgcolor", r.message, r.name)  # no table buttons

	def test_every_message_renders_with_greeting(self):
		from frappe.email.doctype.notification.notification import get_context
		# The trial-countdown bodies gate on the site being Active (workstream B):
		# give the fake contract a provisioning row so the gate opens for the render.
		prov = frappe.get_doc({
			"doctype": "MZ Tenant Provisioning", "contract": "CON-X", "tenant_slug": "x",
			"site_name": "x.erp.mozeconomia.co.mz", "status": "Active",
		})
		prov.flags.ignore_links = True
		prov.insert(ignore_permissions=True)
		self.addCleanup(lambda: frappe.delete_doc("MZ Tenant Provisioning", prov.name, force=True, ignore_permissions=True))
		docs = {
			"Contract": frappe._dict({"doctype": "Contract", "name": "CON-X", "party_name": "Empresa X", "mz_contact_name": "Ana Sitoe",
			                          "mz_account_manager": None, "mz_tenant_url": "x.erp.mozeconomia.co.mz", "start_date": nowdate(),
			                          "mz_subscription_plan": TEST_PLAN, "contact_email": "a@example.com", "is_signed": 0, "mz_billing_start": nowdate()}),
			"MZ Signup": frappe._dict({"doctype": "MZ Signup", "name": "SGN-X", "full_name": "Ana Sitoe", "email": "a@example.com",
			                           "company_name": "Empresa X", "subdomain": "x", "resume_token": "t", "plan": TEST_PLAN}),
		}
		for r in frappe.get_all("Notification", filters={"name": ("like", "AI SaaS%"), "document_type": ("in", list(docs))},
		                        fields=["name", "document_type", "message", "channel"]):
			html = frappe.render_template(r.message, {"doc": docs[r.document_type], "alert": None, "comments": None})
			self.assertNotIn("{{", html, r.name)
			if r.channel == "Email":
				self.assertRegex(html, r"(Bom dia|Boa tarde|Boa noite)( Ana)?,", r.name)
