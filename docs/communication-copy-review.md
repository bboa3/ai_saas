# Communication copy review — purpose vs. copy

Reviewed 2026-08-28 against `notifications_review.csv`. Billing notifications (Fatura, SMS Fatura, Nota de Crédito, Recibo, aviso de faturação) and the pre-existing dunning set are out of scope by decision; the dunning messages added on 2026-08-28 are included. Nothing here has been applied — every "Proposed" block is a decision for the business.

**How to read a verdict.** ✔ keep · ◐ keep, fix the noted lines · ✖ rewrite (purpose and copy disagree).

---

## Cross-cutting findings (fix once, everywhere)

| # | Finding | Why it matters | Decision needed |
|---|---|---|---|
| X1 | **Contract-anchored emails greet the company, not a person.** `Olá {{ doc.party_name }}` renders as "Olá MozEconomia, SA" in every Trial, Activated, Suspended, Archived, Reactivated and Pós-Contrato email. Signup emails greet `full_name` correctly. | A greeting addressed to a legal entity reads as automated and is the first line of every message after signup. | Add a Contract field `mz_contact_name` (set at signup from `full_name`, editable on submitted contracts, backfilled from the Customer's primary Contact) and greet the person; keep the company name in the body where it carries meaning ("a conta da {{ company }}"). |
| X2 | **Two brand voices.** Signup/Trial/lifecycle: "Olá … Equipa MozEconomia Cloud", sign-off "Com boas energias". Pós-Contrato: "Com os melhores cumprimentos, A equipa MozEconomia Cloud" + corporate footer "MOZ ECONOMIA S.A. • Soluções de contabilidade e gestão empresarial" + logo block. Dunning: "Prezado …, Com os melhores cumprimentos". | The same customer receives three registers in one month. | Pick one salutation (recommended: *Olá {nome}*) and one sign-off for relationship emails; keep *Prezado / Com os melhores cumprimentos* only for dunning, where formality is the point. Decide whether "Com boas energias" is brand or accident. |
| X3 | **Spelling: "factura" vs "fatura".** Signup, Trial and lifecycle write *factura*; Pós-Contrato and dunning write *fatura*. | Inconsistent in the same thread; the Autoridade Tributária and Mozambican usage keep the *c*. | Recommended: *factura / facturação* everywhere in customer copy (matches the print formats "Fatura (MZ)"? — those print-format names would stay). |
| X4 | **Two support addresses.** `cloud@mozeconomia.co.mz` in relationship emails, `contacto@mozeconomia.co.mz` in dunning. | A customer replying to a dunning email lands in a different inbox than the one that knows them. | Intentional (billing desk vs support) → keep and say so; accidental → one address. |
| X5 | **Every Trial email ends with the same 3-line block** (activate button · call button · "A sua conta: … Activar não custa nada — a facturação só começa em …"). Over five emails in 14 days the block becomes wallpaper. | The line "Activar não custa nada" is the single most important sentence of the trial and it is buried in grey 13-px text. | Promote it into the body of −7 and −3 once each; keep the buttons; drop the grey repeat. |
| X6 | **"Marcar uma chamada" is offered in 11 messages.** | Fine as an escape hatch, but the emails never say what the call *is* (20 min, we do the first invoice / setup with you, no cost). Where the call is the main CTA (Dia 10, −7 not-logged-in) that sentence must be present. | Standard one-liner: "20 minutos, por vídeo ou telefone — emitimos a primeira factura consigo, sem custo." |

---

## Signup

### Lead Nurture – Dia 0 (on record creation, status Started) — ✖ rewrite
**Purpose.** The person just typed name/email/phone on step 1 and may or may not finish. This email must (1) confirm "we have you", (2) put the *resume link* in their hand for later, (3) give one reason to finish now. It is **not** a sales brochure — they already came to us.
**Copy today.** Opens "Prezado(a)", then a five-bullet product list (facturas certificadas, stock, salários, relatórios, suporte) before the link. The resume link is introduced with "Se ainda não terminou o registo" — but at Dia 0 *nobody* has finished; the record is created on step 1. Length ~190 words.
**Proposed.**
> **Assunto:** O seu registo na MozEconomia Cloud — guarde esta ligação
>
> Olá {{ nome }},
> O registo da {{ empresa or "sua empresa" }} está guardado. Faltam dois passos — o NUIT e o endereço da conta — e a sua conta fica pronta em minutos.
> [Continuar o meu registo]
> Esta ligação leva-o exactamente ao passo onde parou, em qualquer dispositivo, sem palavra-passe. Guarde este email.
> O que recebe ao terminar: uma conta MozEconomia Cloud com facturação certificada pela Autoridade Tributária, pronta a emitir a primeira factura no mesmo dia — e {{ dias }} dias para experimentar tudo sem qualquer pagamento.
> Precisa de ajuda? cloud@mozeconomia.co.mz · WhatsApp +258 87 4444 645
> Equipa MozEconomia Cloud

### já tem uma conta (duplicate email) — ◐
**Purpose.** Stop a confused or forgetful person from creating a second account, and route them: forgot → we help; new company → continue.
**Copy today.** Correct and short. Missing: the site address they already have (we know it), a direct password-reset path, sign-off.
**Proposed.** Add "A sua conta está em **{{ site_name }}**. Para recuperar o acesso: [Repor palavra-passe]" when the existing tenant is known; keep the two paths; close with the standard footer.

### Lead Nurture – Dia 3 — ✔
**Purpose.** Light nudge: reservation is still yours, two minutes to finish. Achieves it. Subject "O seu subdomínio continua reservado" is specific and honest.

### Lead Nurture – Dia 10 — ◐
**Purpose.** Convert doubt into a conversation. Copy does this. Add the X6 one-liner so "chamada curta" has a shape, and make the call the *only* button (the resume link as text) — two equal buttons dilute the one purpose.

### Lead Nurture – Dia 20 — ◐
**Purpose.** Now the closer (Dia 30 was removed): last reminder, lowest-effort offer (we set it up with you).
**Fix.** Say it is the last message: "Este é o último lembrete que enviamos — a ligação continua a funcionar, quando quiser." Removes the empty "vai expirar" threat and leaves the door open honestly.

---

## Account delivery

### Entrega da Conta (end of provisioning) — ◐
**Purpose.** One job: get them *inside* now (set password, log in). Secondary: set the trial frame (until when, what happens, that activating costs nothing).
**Copy today.** The primary button is right. But the trial branch then spends four sentences on *suspension* and *activation* with two more buttons — three CTAs in a welcome email, and the word "suspenso" appears before the customer has ever logged in.
**Proposed (trial branch).**
> Tem até **{{ trial_end }}** para experimentar tudo — sem qualquer pagamento. Nada do que registar se perde: quando activar, continua exactamente onde estava, e a facturação só começa em {{ trial_end }}.
> Amanhã enviamos um guia de 5 minutos para a primeira factura. Se preferir fazê-la connosco: [marcar 20 minutos] (texto, não botão).
> Plano escolhido: {{ plan }} — pode alterá-lo quando activar.
Keep the single button *Definir a minha palavra-passe e entrar*. Move "Activar a minha conta" out of this email entirely — the Day-2 and −7 emails carry it.

### problema temporário na conta (provisioning failed) — ✖ rewrite
**Purpose.** A person is waiting for a site that did not appear. Reduce anxiety: nothing is lost, no action needed, a human will contact them, by when, and how to reach us meanwhile.
**Copy today.** Three sentences, no greeting style, no timeline, no contact, no signature.
**Proposed.**
> **Assunto:** A sua conta MozEconomia Cloud está a demorar mais do que o normal
>
> Olá {{ nome }},
> A preparação da conta da {{ empresa }} encontrou um problema técnico do nosso lado. O seu registo está guardado e não precisa de fazer nada.
> Um membro da equipa entra em contacto consigo **hoje** (dias úteis, até às 18h) para lhe entregar o acesso. Se preferir falar já: WhatsApp +258 87 4444 645.
> Pedimos desculpa pela espera.
> Equipa MozEconomia Cloud

---

## Trial

### Trial – Primeira factura (day 2) — ✔
**Purpose.** Produce the first invoice-day — the strongest predictor of conversion. Three steps, one button into the site, the call as fallback, the trial frame in one line. Check the menu path against the pt-MZ desk labels once ("Vendas → Factura de Venda → Nova").

### Trial – 7 dias — ◐
**Purpose.** Halfway point. Depending on usage: (invoiced) make activating feel like the obvious continuation; (logged in, no invoice) push the first invoice this week; (never logged in) rescue.
**Copy today.** The branches are right. Two fixes: the *invoiced* branch should say what they will keep in concrete terms ("as {{ n }} facturas que já emitiu, os clientes e artigos"), which the snapshot provides (`invoice_count`); and per X5 "Activar não custa nada — a facturação só começa em {{ date }}" belongs in the body, once, not in the grey footer.

### Trial – 3 dias — ◐
**Purpose.** Decision email. Two audiences: (invoiced) activate — continuity; (not invoiced) the doubt is the obstacle — call.
**Copy today.** Subject "3 dias: decida sem pressa, mas decida" is clever but slightly bossy for a customer who has not decided in our favour; the not-invoiced branch offers only a call — it should still offer the product ("ou emita uma factura de teste hoje — é a forma mais rápida de decidir").
**Proposed subject.** "3 dias: a conta da {{ empresa }} continua como está se activar até {{ date }}".

### Trial – Último dia amanhã — ✔ (given the decision to keep −1 and 0)
Straight, factual, one sentence of reassurance. Subject "Amanhã a sua conta é suspensa" is the correct urgency for D−1.

### Trial – Hoje — ◐
**Purpose.** Last chance today; make replying as easy as activating.
**Fix.** "suspenso amanhã de manhã" — the engine runs on the scheduler's daily slot; say "amanhã" only. The `[SE invoiced:] Tem facturas emitidas nesta conta.` fragment is a stub — replace with the concrete number: "As {{ n }} facturas que emitiu ficam guardadas, mas a partir de amanhã não emite mais."

### SMS Trial Hoje — ◐
**Purpose.** Reach the phone on the last day with a tappable link.
**Copy today.** ~200 characters with a long tokenised URL and the company name → 2 segments; "Duvidas: WhatsApp…" adds a second ask.
**Proposed (≤160 with a short host).** "MozEconomia Cloud: o seu periodo experimental termina hoje. Active em 1 minuto e continue sem interrupcao: {{ url }}" — and consider a short redirect (`/a/<token>`) so the link fits.

---

## Activation / onboarding

### Conta Activada — ✔
**Purpose.** Confirm the decision, remove the "what did I just sign up for" doubt: plan, when billing starts, when the first invoice arrives, how to pay, how to change things. Copy does all five. Optional: one line inviting the team ("Convide a sua equipa: Configurações → Utilizadores") — the breadth signal we measure.

### Pós-Contrato – Dia 3 — ✖ rewrite
**Purpose (redefined).** The customer is now *paying*. Day 3 should turn a paying customer into an embedded one — not ask whether they have issued a first invoice (most converted trials have issued a dozen). Purpose: "get the most out of it this week": one concrete next step chosen by usage.
**Copy today.** Assumes no invoice; generic "estamos disponíveis"; corporate footer (X2).
**Proposed (snapshot-aware, like the trial emails).**
> **Assunto:** Três coisas que valem a pena fazer esta semana
> Olá {{ nome }},
> {% if invoiced %}A {{ empresa }} já emite facturas na MozEconomia Cloud — agora vale a pena fechar o ciclo:{% else %}A conta da {{ empresa }} está activa — a primeira factura é o melhor primeiro passo (leva 5 minutos):{% endif %}
> 1. **Registe os pagamentos** que recebe — o extracto de cada cliente fica em dia sozinho.
> 2. **Convide quem factura consigo** — cada pessoa com o seu utilizador (Configurações → Utilizadores).
> 3. **Confirme os dados fiscais** que saem nas facturas — NUIT, endereço e logótipo (Configurações → Empresa).
> Prefere fazê-lo connosco? 20 minutos, sem custo: [Marcar]
> Equipa MozEconomia Cloud

### Pós-Contrato – Dia 5 — ◐ (or merge into Dia 3 — the recommended option, declined for now)
**Purpose.** One configuration tip that prevents a real problem (fiscal data on invoices). The tip is good; the frame "antes de emitir a primeira fatura" is wrong for a converted trial. Fix the frame: "Vale a pena confirmar…". If Dia 3 adopts the three-step proposal above, Dia 5 duplicates step 3 and should be removed.

### Pós-Contrato – Dia 33 — ✔
**Purpose.** First-renewal feedback and retention touch. Copy now asks for a reply or a call — right. Subject fine.

---

## Dunning (added 2026-08-28 only)

### Aviso de Atraso 7 dias / 15 dias — ✔
Same skeleton as the existing *Aviso de Suspensão* (Prezado, table, methods, contacts) so they read as one series; the 15-day message adds "se houver alguma dificuldade, diga-nos" — the right tone at the midpoint. Note: "33 dias", "18 dias", "3 dias" are literals tied to `overdue_days_to_suspend = 33`.

### Aviso de Suspensão em 3 dias — ✔
States consequence, date, and that data stays intact; the "perda definitiva" claim is gone.

### SMS Vencimento Hoje / Atraso 7 dias / Suspensão em 3 dias — ◐
Each names the invoice and the amount. "Vencimento Hoje" packs two payment methods and a NIB into an SMS — hard to act on from a phone; propose: "MozEconomia Cloud: a factura {{ n }} de {{ valor }} vence hoje. Pague por E-Mola 87 4444 645 (ref. {{ n }}) ou responda para receber o NIB." The two later SMS are fine.

---

## Suspension / archive

### Conta Suspensa — ✔
Purpose (why, data safe, way back, archive warning) and copy agree in all three branches. One wording check in the trial branch: "a facturação só começa nessa data" — correct (billing starts on the activation day), but say it: "só começa no dia em que activar".

### Conta Arquivada — ◐
Purpose: close the loop with dignity and keep the door open. Add the one fact the customer will ask: for how long the backup is kept (define the policy — e.g. 12 months) and whether restore has a cost.

### Conta Reactivada — ✔
Short, factual, two branches. Fine.

---

## Recommended order of work

1. X1 (greet a person) — touches every post-signup email; do first.
2. X2/X3 (one voice, one spelling) — a single pass over the fixture and the two template dicts.
3. Rewrites: Dia 0, problema temporário, Pós-Contrato Dia 3 (snapshot-aware), Entrega trial branch.
4. Smaller fixes: Dia 10/20, −7 and −3 bodies (X5), Hoje stub, SMS Trial Hoje length, SMS Vencimento Hoje, Arquivada backup policy.
