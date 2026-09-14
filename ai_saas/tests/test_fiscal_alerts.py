"""Content layer 1 — the Alerta Fiscal (docs/content-layers-implementation.md):
business days over a Holiday List, the deadline rule, the send window, the
recipient filters. Mail is patched; nothing leaves the process."""

from datetime import date
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from ai_saas.saas import fiscal_alerts
from ai_saas.utils.dates import subtract_business_days

HOLIDAY_LIST = "_Test Alerta Fiscal Holidays"
OB = "_Test Obrigação Alerta"
TEST_CODE = "_TESTCODE"
LEADS = {
	"normal": "alerta-normal@example.com",
	"isento": "alerta-isento@example.com",
	"other": "alerta-outro-sector@example.com",
	"left": "alerta-saiu@example.com",
	"unflagged": "alerta-sem-flag@example.com",
}


class TestFiscalAlerts(FrappeTestCase):
	def setUp(self):
		self._cleanup()
		if not frappe.db.exists("Holiday List", HOLIDAY_LIST):
			frappe.get_doc(
				{
					"doctype": "Holiday List",
					"holiday_list_name": HOLIDAY_LIST,
					"from_date": "2026-01-01",
					"to_date": "2026-12-31",
					"holidays": [{"holiday_date": "2026-09-07", "description": "Lusaka"}],  # a Monday
				}
			).insert(ignore_permissions=True)
		# Every shipped segment carries IVA/IRPS/INSS/IRPC, so the segment filter is proved
		# with a code only one segment has: a row appended for the test and removed after.
		self.segments = frappe.get_all("Segment Intelligence Map", pluck="name", order_by="name")
		self.segment_with_iva, self.other_segment = self.segments[0], self.segments[1]
		seg = frappe.get_doc("Segment Intelligence Map", self.segment_with_iva)
		seg.append("conformidade_mz", {"conformidade": TEST_CODE})
		seg.save(ignore_permissions=True)
		for key, email in LEADS.items():
			frappe.get_doc(
				{
					"doctype": "Lead",
					"first_name": f"Teste {key}",
					"email_id": email,
					"status": "Lead",
					"mz_fiscal_alerts": 0 if key == "unflagged" else 1,
					"unsubscribed": 1 if key == "left" else 0,
					"mz_tax_regime": "Isento" if key == "isento" else "Normal (16%)",
					"mz_segment": self.other_segment if key == "other" else self.segment_with_iva,
				}
			).insert(ignore_permissions=True)
		self.ob = frappe.get_doc(
			{
				"doctype": "MZ Fiscal Obligation",
				"title": OB,
				"code": "IVA",
				"periodicity": "Monthly",
				"deadline_day": 20,
				"applies_to_all_segments": 1,
				"requires_vat": 1,
				"alert_subject": "IVA até {{ deadline }}",
				"alert_body": "<p>Olá {{ lead.first_name }}: IVA até {{ deadline }}.</p>",
				"reviewed_by": "Contabilista Teste",
				"reviewed_on": "2026-09-01",
				"enabled": 1,
			}
		).insert(ignore_permissions=True)
		frappe.db.set_single_value("MZ SaaS Settings", "holiday_list", HOLIDAY_LIST)
		frappe.db.set_single_value("MZ SaaS Settings", "fiscal_alert_lead_days", 3)
		frappe.db.commit()

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		for email in LEADS.values():
			for l in frappe.get_all("Lead", {"email_id": email}, pluck="name"):
				frappe.delete_doc("Lead", l, force=True, ignore_permissions=True)
		frappe.delete_doc(
			"MZ Fiscal Obligation", OB, force=True, ignore_missing=True, ignore_permissions=True
		)
		frappe.db.delete(
			"Conformidade MZ", {"parenttype": "Segment Intelligence Map", "conformidade": TEST_CODE}
		)
		frappe.delete_doc(
			"Holiday List", HOLIDAY_LIST, force=True, ignore_missing=True, ignore_permissions=True
		)
		frappe.db.set_single_value("MZ SaaS Settings", "holiday_list", None)
		frappe.db.commit()

	# ---- dates ------------------------------------------------------------------

	def test_business_days_skip_weekends_and_holidays(self):
		# Thu 2026-09-10 minus 3 business days: Wed 9, Tue 8, (Mon 7 holiday, Sun, Sat) Fri 4.
		self.assertEqual(subtract_business_days(date(2026, 9, 10), 3, HOLIDAY_LIST), date(2026, 9, 4))
		self.assertEqual(subtract_business_days(date(2026, 9, 10), 0, HOLIDAY_LIST), date(2026, 9, 10))
		# Without a list weekends are still not business days.
		self.assertEqual(subtract_business_days(date(2026, 9, 7), 1, None), date(2026, 9, 4))

	def test_next_deadline_rules(self):
		ob = frappe._dict(periodicity="Monthly", deadline_day=20)
		self.assertEqual(fiscal_alerts.next_deadline(ob, date(2026, 9, 6)), date(2026, 9, 20))
		self.assertEqual(fiscal_alerts.next_deadline(ob, date(2026, 9, 21)), date(2026, 10, 20))
		self.assertEqual(fiscal_alerts.next_deadline(ob, date(2026, 12, 25)), date(2027, 1, 20))
		ob = frappe._dict(periodicity="Monthly", deadline_day=31)
		self.assertEqual(fiscal_alerts.next_deadline(ob, date(2026, 2, 1)), date(2026, 2, 28))  # clamped
		ob = frappe._dict(periodicity="Annual", deadline_day=31, deadline_month=5)
		self.assertEqual(fiscal_alerts.next_deadline(ob, date(2026, 6, 1)), date(2027, 5, 31))
		ob = frappe._dict(periodicity="Quarterly", deadline_day=15, deadline_month=2)
		self.assertEqual(
			fiscal_alerts.next_deadline(ob, date(2026, 9, 6)), date(2026, 11, 15)
		)  # Feb, May, Aug, Nov

	# ---- the job ----------------------------------------------------------------

	def _run(self, today, dry_run=False):
		with patch("ai_saas.saas.fiscal_alerts.frappe.sendmail") as sendmail:
			summary = fiscal_alerts.send_due_alerts(today=today, dry_run=dry_run)
		return summary[OB], sendmail

	def test_window_recipients_and_idempotence(self):
		# Deadline Sun 2026-09-20 → alert date 3 business days earlier: Thu 17 (Fri 18, Thu 17... walk: 18, 17, 16 → 16).
		out, sendmail = self._run(date(2026, 9, 10))
		self.assertEqual((out["sent"], out["reason"]), (0, "not in window"))
		self.assertEqual(sendmail.call_count, 0)

		out, sendmail = self._run(date(2026, 9, 18))  # inside the window, first run
		recipients = sorted(c.kwargs["recipients"][0] for c in sendmail.call_args_list)
		# Isento excluded (requires_vat), unsubscribed excluded, unflagged excluded; the other
		# sector still receives it (applies_to_all_segments).
		self.assertEqual(recipients, sorted([LEADS["normal"], LEADS["other"]]))
		self.assertEqual(out["sent"], 2)
		call = sendmail.call_args_list[0].kwargs
		self.assertEqual(call["reference_doctype"], "Lead")
		self.assertIn("20/09/2026", call["subject"])
		self.assertIn("/sair?l=", call["message"])
		self.assertEqual(
			str(frappe.db.get_value("MZ Fiscal Obligation", OB, "last_alerted_deadline")), "2026-09-20"
		)

		out, sendmail = self._run(date(2026, 9, 19))  # next day, same deadline: nothing
		self.assertEqual((out["sent"], out["reason"]), (0, "already sent"))
		out, sendmail = self._run(date(2026, 10, 1))  # next month: not yet
		self.assertEqual(out["reason"], "not in window")

	def test_segment_filter_and_review_gate(self):
		self.ob.db_set({"applies_to_all_segments": 0, "requires_vat": 0, "code": TEST_CODE})
		_, sendmail = self._run(date(2026, 9, 18))
		recipients = sorted(c.kwargs["recipients"][0] for c in sendmail.call_args_list)
		self.assertEqual(
			recipients, sorted([LEADS["normal"], LEADS["isento"]])
		)  # the other sector lacks the code
		# An obligation that lost its review never sends, enabled or not.
		self.ob.db_set({"reviewed_on": None, "last_alerted_deadline": None})
		with patch("ai_saas.saas.fiscal_alerts.frappe.sendmail") as sendmail:
			summary = fiscal_alerts.send_due_alerts(today=date(2026, 9, 18))
		self.assertNotIn(OB, summary)
		self.assertEqual(sendmail.call_count, 0)

	def test_dry_run_sends_nothing(self):
		out, sendmail = self._run(date(2026, 9, 18), dry_run=True)
		self.assertEqual((out["sent"], out["reason"], sendmail.call_count), (2, "dry run", 0))
		self.assertFalse(frappe.db.get_value("MZ Fiscal Obligation", OB, "last_alerted_deadline"))

	def test_enabling_requires_review(self):
		doc = frappe.get_doc("MZ Fiscal Obligation", OB)
		doc.reviewed_on = None
		self.assertRaises(frappe.ValidationError, doc.save)
