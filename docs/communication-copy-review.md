# MozEconomia Cloud — communication language and message objectives

Status: **applied 2026-08-28** to every customer-facing message in `ai_saas` (fixtures/notification.json, the five Email Templates in `install.py`, the "já tem uma conta" email in `api/signup.py`). Billing documents (Fatura Emitida, Nota de Crédito, Recibo, SMS Fatura Emitida, Aviso de Faturação) were left untouched by decision. The scheduled emails go out at 08:00 Africa/Maputo (`install.ensure_daily_alerts_hour`).

Goal of every message: more Cloud sales and a better customer experience. A message sells when it is time to sell, and is professional always.

## The language

One voice, two registers — chosen by what the message does.

| | Relationship register | Formal register |
|---|---|---|
| Used for | Signup, delivery, trial, activation, onboarding, suspension / archive / reactivation | Invoices, receipts, credit notes, dunning |
| Greeting | `{{ mz_greeting(name) }}` → **Bom dia / Boa tarde / Boa noite {first name},** from the sending time | **Prezado(a) {contact person},** — the person (`contact_display`), company name in the body |
| Sign-off | `{{ mz_signature(user) }}` → **Com boas energias,** account manager's name (`Contract.mz_account_manager`, else Settings › default sales user), **Equipa MozEconomia Cloud** | **Com os melhores cumprimentos,** Equipa MozEconomia Cloud |
| Inbox | `cloud@mozeconomia.co.mz` · WhatsApp +258 87 4444 645 | `contacto@mozeconomia.co.mz` |
| Voice | Warm, direct, confident; short sentences; we say what we do | Precise, courteous, no drama; facts, amounts, dates, consequence — once |
| Sells by | One benefit + proof from their own account (invoice count from the usage snapshot) + one CTA | Never sells; makes paying easy: one reference, two methods |

Fixed rules for both registers:
- **One CTA per message**, a branded pill link (`_BTN`); secondary actions are green inline links (`_LINK`), never a second pill and never a table button.
- **Speak to a person**: `Contract.mz_contact_name` (a read-only mirror of the Customer's primary Contact since 2026-08-30); never "Olá Empresa, SA".
- **Standard lines**, written once in `install.py`: `SUPPORT_LINE`, `TRIAL_PROMISE` ("Activar não custa nada — a facturação começa no dia em que activar"), `CALL_OFFER` ("20 minutos connosco: emitimos a primeira factura consigo, sem custo").
- **Mozambican Portuguese**: factura, facturação, activar, contacto, utilizador. In the trial group the account *pausa*; *suspensa* is used only when it is the actual state (lifecycle, dunning).
- **Subjects state a fact or a benefit**, never a command ("decida" is gone).
- A lead **never** hears about a provisioning problem — the team does (`_send_failure_alert`).
- Cobrança lives on the invoice; the D+1…D+30 notices exist by decision (2026-08-28) and are written as facts, not threats.

## Objective of each message

| Message | Objective |
|---|---|
| Lead Nurture Dia 0 | We have you; here is the way back in; one reason to finish today |
| Dia 3 | Remove friction: two minutes, the address is yours, first factura this week |
| Dia 10 | Answer the real doubt ("does it fit my business?") — see for yourself, or 20 minutes with a person |
| Dia 20 | Biggest offer (setup session); the link stays valid — remarketing comes later, so it never says "last email" |
| Já tem uma conta | Back into the account they already have (site link + password reset) |
| Entrega da Conta | Get them logged in today; trial branch adds the first-factura nudge and TRIAL_PROMISE |
| Trial — Primeira factura (D+2) | First invoice issued this week (the moment the product proves itself; feeds the engagement score) |
| Trial −7 | Turn use into a decision early: "já emitiu N facturas" |
| Trial −3 | Zero cost of deciding: activar leva um minuto e não muda a data em que começa a pagar |
| Trial −1 | Avoid the interruption |
| Trial Hoje + SMS | Last call, warm; reply and we answer today |
| Conta Activada | Confirm the decision; plan, billing start, how to pay |
| Pós-Contrato Dia 3 | Deepen use in week one: register payments, invite the team, confirm fiscal data |
| Pós-Contrato Dia 5 | One honest offer of help from a person |
| Pós-Contrato Dia 33 | First-month feedback, retention conversation |
| Lembrete 2 / 3, SMS Vencimento Hoje | Get paid inside the payment window; facts once; SMS points to the invoice email for the bank details |
| Aviso D+1 / Atraso 7 / Atraso 15 | State the overdue fact and the D+33 date; at 15, "se houver dificuldade, diga-nos" |
| Suspensão em 3 dias + SMS | Action notice: date, invoice, data intact, "if you already paid send the proof" |
| Conta Suspensa | Why, data safe, the one way back, archive warning |
| Conta Arquivada | Backup made, kept 12 months (AT retention is 10 years; 12 months is the customer promise), how to restore |
| Conta Reactivada | You are back; nothing lost; next step |

## Mechanics
- Helpers: `ai_saas/utils/jinja.py` (`mz_greeting`, `mz_first_name`, `mz_signature`) — available in every Notification and Email Template.
- Code-sent emails pass `greeting`, `first_name`, `signature` in context (`lifecycle_mail.build_context`, `provisioning._welcome_email_context`).
- Email Templates are create-if-missing; to ship a copy revision to a site: `bench --site X execute ai_saas.install.push_email_templates`.
- Tests: `tests/test_messaging.py::TestCommunicationLanguage`.
