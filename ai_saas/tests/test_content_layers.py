"""Content layers 2 and 3 and the stage machine in front of the form
(docs/content-layers-implementation.md): subscription → Lead + Opportunity at
Subscribed, the guide with its per-company link, opening it → Aware, /registo
advancing the same Opportunity, suppression, the auxiliary cap → Dormant."""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ai_saas.api import content as api
from ai_saas.saas import content, crm

EMAIL = "camadas-teste@example.com"
PAGE_TITLE = "_Test Guia Sectorial"
AUX_TEMPLATE = "_Test Email Auxiliar"


class TestContentLayers(FrappeTestCase):
	def setUp(self):
		self._cleanup()
		self.segment = frappe.get_all("Segment Intelligence Map", pluck="name", order_by="name")[0]
		self.page = frappe.get_doc(
			{
				"doctype": "Web Page",
				"title": PAGE_TITLE,
				"route": "guia-teste",
				"published": 1,
				"content_type": "HTML",
				"main_section_html": "<p>Guia de teste</p>",
				"meta_description": "Três linhas sobre o guia.",
				"mz_segments": [{"segment": self.segment}],
			}
		).insert(ignore_permissions=True)
		if not frappe.db.exists("Email Template", AUX_TEMPLATE):
			frappe.get_doc(
				{
					"doctype": "Email Template",
					"name": AUX_TEMPLATE,
					"subject": "Prazo: {{ doc.company_name }}",
					"use_html": 1,
					"response_html": "<p>{{ greeting }} novidade. <a href='{{ unsubscribe_url }}'>sair</a></p>",
				}
			).insert(ignore_permissions=True)
		frappe.db.set_single_value("MZ SaaS Settings", "auxiliary_max_without_signal", 2)
		frappe.db.commit()

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		for o in frappe.get_all("Opportunity", {"contact_email": EMAIL}, pluck="name"):
			for t in frappe.get_all(
				"ToDo", {"reference_type": "Opportunity", "reference_name": o}, pluck="name"
			):
				frappe.delete_doc("ToDo", t, force=True, ignore_permissions=True)
			frappe.delete_doc("Opportunity", o, force=True, ignore_permissions=True)
		for l in frappe.get_all("Lead", {"email_id": EMAIL}, pluck="name"):
			frappe.delete_doc("Lead", l, force=True, ignore_permissions=True)
		for s in frappe.get_all("MZ Signup", {"email": EMAIL}, pluck="name"):
			frappe.delete_doc("MZ Signup", s, force=True, ignore_permissions=True)
		for p in frappe.get_all("Web Page", {"title": PAGE_TITLE}, pluck="name"):
			frappe.delete_doc("Web Page", p, force=True, ignore_permissions=True)
		frappe.delete_doc(
			"Email Template", AUX_TEMPLATE, force=True, ignore_missing=True, ignore_permissions=True
		)
		frappe.db.set_single_value("MZ SaaS Settings", "auxiliary_max_without_signal", None)
		frappe.db.commit()

	def _subscribe(self, **overrides):
		args = {
			"full_name": "Ana Teste",
			"email": EMAIL,
			"company_name": "Padaria Teste",
			"segment": self.segment,
			"tax_regime": "Normal (16%)",
		}
		args.update(overrides)
		with patch("ai_saas.saas.content.frappe.sendmail") as sendmail:
			out = api._subscribe(**args)
		return out, sendmail

	def _opp(self):
		return frappe.db.get_value(
			"Opportunity",
			{"contact_email": EMAIL},
			[
				"name",
				"sales_stage",
				"status",
				"source",
				"mz_guide_sent_on",
				"mz_guide_opened_on",
				"mz_aux_sent",
			],
			as_dict=True,
			order_by="creation desc",
		)

	# ---- layer 1 entry + layer 2 delivery ----------------------------------------

	def test_subscription_creates_lead_opportunity_and_sends_the_guide(self):
		out, sendmail = self._subscribe()
		self.assertEqual(out, {"ok": 1})
		lead = frappe.db.get_value(
			"Lead",
			{"email_id": EMAIL},
			["name", "mz_fiscal_alerts", "mz_segment", "mz_tax_regime", "company_name", "source"],
			as_dict=True,
		)
		self.assertEqual(
			(lead.mz_fiscal_alerts, lead.mz_segment, lead.mz_tax_regime, lead.company_name, lead.source),
			(1, self.segment, "Normal (16%)", "Padaria Teste", "Alerta Fiscal"),
		)
		opp = self._opp()
		self.assertEqual(
			(opp.sales_stage, opp.status, opp.source), (crm.STAGE_SUBSCRIBED, "Open", "Alerta Fiscal")
		)
		self.assertEqual(str(opp.mz_guide_sent_on), frappe.utils.nowdate())
		self.assertEqual(sendmail.call_count, 1)
		kw = sendmail.call_args.kwargs
		self.assertEqual(
			(kw["recipients"], kw["reference_doctype"], kw["reference_name"]),
			([EMAIL], "Opportunity", opp.name),
		)
		self.assertIn(self.segment, kw["subject"])
		url = content.guide_url(opp.name)
		self.assertIn("/guia-teste?o=" + opp.name + "&t=", url)
		self.assertIn(url, kw["message"])
		self.assertIn("/sair?l=" + lead.name, kw["message"])
		self.assertNotIn("{{", kw["message"])

	def test_second_subscription_is_a_quiet_no_op(self):
		self._subscribe()
		out, sendmail = self._subscribe(company_name="Padaria Teste, LDA")
		self.assertEqual(out, {"ok": 1})
		self.assertEqual(sendmail.call_count, 0)
		self.assertEqual(frappe.db.count("Opportunity", {"contact_email": EMAIL}), 1)
		self.assertEqual(
			frappe.db.get_value("Lead", {"email_id": EMAIL}, "company_name"), "Padaria Teste, LDA"
		)

	def test_no_published_guide_means_no_email_and_no_anchor(self):
		self.page.db_set("published", 0)
		_, sendmail = self._subscribe()
		self.assertEqual(sendmail.call_count, 0)
		opp = self._opp()
		self.assertEqual(opp.sales_stage, crm.STAGE_SUBSCRIBED)
		self.assertIsNone(opp.mz_guide_sent_on)
		self.assertEqual(content.guide_url(opp.name), "")

	def test_subscription_of_a_lead_already_in_the_funnel_only_flags(self):
		lead = frappe.get_doc(
			{"doctype": "Lead", "first_name": "Já Cliente", "email_id": EMAIL, "status": "Lead"}
		).insert(ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Opportunity",
				"opportunity_from": "Lead",
				"party_name": lead.name,
				"company": frappe.db.get_single_value("Global Defaults", "default_company"),
				"sales_stage": crm.STAGE_ACCOUNT_CREATED,
				"contact_email": EMAIL,
			}
		).insert(ignore_permissions=True)
		_, sendmail = self._subscribe()
		self.assertEqual(sendmail.call_count, 0)
		self.assertEqual(frappe.db.count("Opportunity", {"contact_email": EMAIL}), 1)
		self.assertEqual(self._opp().sales_stage, crm.STAGE_ACCOUNT_CREATED)
		self.assertEqual(frappe.db.get_value("Lead", lead.name, "mz_fiscal_alerts"), 1)

	# ---- layer 2 signal ----------------------------------------------------------

	def test_opening_the_guide_moves_to_aware_and_bad_tokens_do_nothing(self):
		self._subscribe()
		opp = self._opp()
		self.assertEqual(api._guide_opened(opp.name, "nope"), {"ok": 0})
		self.assertEqual(self._opp().sales_stage, crm.STAGE_SUBSCRIBED)
		self.assertEqual(api._guide_opened(opp.name, content.guide_token(opp.name)), {"ok": 1})
		after = self._opp()
		self.assertEqual(after.sales_stage, crm.STAGE_AWARE)
		self.assertTrue(after.mz_guide_opened_on)
		# Dormant comes back to Aware; a trial account is left alone.
		crm.report(opp.name, crm.STAGE_DORMANT)
		self.assertEqual(content.record_guide_open(opp.name), crm.STAGE_AWARE)
		crm.report(opp.name, crm.STAGE_ACCOUNT_CREATED)
		self.assertEqual(content.record_guide_open(opp.name), crm.STAGE_ACCOUNT_CREATED)

	# ---- the form advances the same Opportunity ------------------------------------

	@patch("ai_saas.saas.crm._resend_day_zero")
	def test_registo_advances_the_content_opportunity(self, resend):
		self._subscribe()
		before = self._opp()
		signup = frappe.get_doc(
			{
				"doctype": "MZ Signup",
				"status": "Started",
				"full_name": "Ana Teste",
				"email": EMAIL,
				"phone": "841234567",
			}
		).insert(ignore_permissions=True)
		crm.enter_crm(signup)
		self.assertEqual(frappe.db.count("Opportunity", {"contact_email": EMAIL}), 1)
		after = self._opp()
		self.assertEqual((after.name, after.sales_stage), (before.name, crm.STAGE_FORM_STARTED))
		self.assertEqual(frappe.db.get_value("Opportunity", after.name, "mz_signup"), signup.name)
		resend.assert_called_once_with(
			before.name
		)  # a stage change fires no "New": the resume link goes by hand

	# ---- layer 3 -----------------------------------------------------------------

	def test_auxiliary_cap_moves_to_dormant_and_signals_reset(self):
		self._subscribe()
		opp = self._opp()
		out = content.send_auxiliary(AUX_TEMPLATE, dry_run=1)
		self.assertEqual((out["recipients"], out["sent"]), ([EMAIL], 0))
		with patch("ai_saas.saas.content.frappe.sendmail") as sendmail:
			out = content.send_auxiliary(AUX_TEMPLATE)
		self.assertEqual((out["sent"], out["dormant"]), (1, []))
		kw = sendmail.call_args.kwargs
		self.assertEqual(kw["subject"], "Prazo: Padaria Teste")
		self.assertIn("/sair?l=", kw["message"])
		self.assertEqual(self._opp().mz_aux_sent, 1)
		# A signal resets the count.
		content.record_guide_open(opp.name)
		self.assertEqual(self._opp().mz_aux_sent, 0)
		with patch("ai_saas.saas.content.frappe.sendmail"):
			content.send_auxiliary(AUX_TEMPLATE)
			out = content.send_auxiliary(AUX_TEMPLATE)
		self.assertEqual(out["dormant"], [opp.name])
		self.assertEqual(self._opp().sales_stage, crm.STAGE_DORMANT)
		with patch("ai_saas.saas.content.frappe.sendmail") as sendmail:
			out = content.send_auxiliary(AUX_TEMPLATE)
		self.assertEqual((out["sent"], sendmail.call_count), (0, 0))  # Dormant: only the alert now

	# ---- suppression -------------------------------------------------------------

	def test_unsubscribe_closes_the_content_opportunity_and_flags_the_lead(self):
		self._subscribe()
		lead = frappe.db.get_value("Lead", {"email_id": EMAIL}, "name")
		self.assertRaises(frappe.PermissionError, api._unsubscribe, lead, "nope")
		self.assertEqual(api._unsubscribe(lead, content.unsubscribe_token(lead)), {"ok": 1})
		l = frappe.db.get_value("Lead", lead, ["unsubscribed", "mz_fiscal_alerts"], as_dict=True)
		self.assertEqual((l.unsubscribed, l.mz_fiscal_alerts), (1, 0))
		opp = self._opp()
		self.assertEqual((opp.sales_stage, opp.status), (crm.STAGE_UNSUBSCRIBED, "Lost"))
		self.assertEqual(content.auxiliary_recipients(), [])
		# Subscribing again by hand is consent: a fresh Opportunity, the old one stays Lost.
		self._subscribe()
		self.assertEqual(frappe.db.get_value("Lead", lead, "unsubscribed"), 0)
		self.assertEqual(frappe.db.count("Opportunity", {"contact_email": EMAIL}), 2)
		self.assertEqual(self._opp().sales_stage, crm.STAGE_SUBSCRIBED)

	def test_unsubscribe_leaves_a_trial_stage_alone(self):
		self._subscribe()
		opp = self._opp()
		crm.report(opp.name, crm.STAGE_ACCOUNT_CREATED)
		lead = frappe.db.get_value("Lead", {"email_id": EMAIL}, "name")
		content.suppress(lead)
		self.assertEqual(self._opp().sales_stage, crm.STAGE_ACCOUNT_CREATED)
		self.assertEqual(frappe.db.get_value("Lead", lead, "unsubscribed"), 1)

	# ---- the Day-4 notification --------------------------------------------------

	def test_day_four_notification_shape_and_render(self):
		n = frappe.get_doc("Notification", "AI SaaS - Guia - Dia 4")
		self.assertEqual(
			(n.document_type, n.event, n.date_changed, n.days_in_advance),
			("Opportunity", "Days After", "mz_guide_sent_on", 4),
		)
		self.assertIn('"Cloud - Subscribed"', n.condition)
		self.assertEqual([r.receiver_by_document_field for r in n.recipients], ["contact_email"])
		self._subscribe()
		opp = frappe.get_doc("Opportunity", self._opp().name)
		self.assertTrue(frappe.safe_eval(n.condition, None, {"doc": opp.as_dict()}))
		html = frappe.render_template(n.message, {"doc": opp})
		self.assertNotIn("{{", html)
		self.assertIn("/guia-teste?o=", html)
		self.assertIn("/registo", html)
		self.assertIn("/sair?l=", html)
		crm.report(opp.name, crm.STAGE_FORM_STARTED)
		opp.reload()
		self.assertFalse(
			frappe.safe_eval(n.condition, None, {"doc": opp.as_dict()})
		)  # the form stopped the sequence
