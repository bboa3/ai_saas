# AI subdomain suggestions on /registo

## What is a good slug (the spec behind the prompt)

Grounded in the real tenant inventory (2026-08-30, ~46 live slugs): the single distinctive
brand word dominates (`sanlo`, `climart`, `ndolene` for "Papelaria Ndolene"); two-word
brands are joined, not hyphenated (`maisforte`, `tecnobiza` — only 2 of 46 use a hyphen);
initials are first-class (~25% of tenants: `afc`, `mms`, `omp`); everything is 4–12 chars.

The definition — the answer to "qual é o endereço do vosso sistema?" said out loud:

1. **The distinctive brand word alone is the best slug**: "Sanlo Moçambique, LDA" → `sanlo`.
   Category/legal/geography/filler words are dropped — kept only when the category carries
   the identity ("Farmácia Central" → `farmacia-central`).
2. **Two short brand words: join them** (`maisforte`); hyphenate only when joining hurts
   readability (`bom-apetite`). At most one hyphen.
3. **Initials are a strong candidate when the company is known by them** ("Automatic Fire
   Control" → `afc`). Minimum 3 characters.
4. **Never invent**: no truncations (`farmacia-ctr` ✗), no reordering (`central-farmacia` ✗),
   no made-up prefixes (`fc-farmacia` ✗). Digits only when part of the name.
5. Short: ideally 4–14 characters.
6. The last candidate may append the company's city (`farmacia-central-matola`) — the
   collision distinguisher (MZ company names are nationally unique, so a taken slug means
   "add something"). The city comes from the signup record (derived from the step-2
   address), looked up by resume token — the browser sends `token` with the request.

Keep the `_SYSTEM` prompt in `slug_ai.py` and this section in sync — this is the spec,
the prompt is its implementation.

The step-3 subdomain prefill (`suggest_subdomain`) asks Claude Haiku 4.5 for 3–5 slug
candidates from the company name, takes the first one that is valid **and** available,
and falls back to the deterministic `slug_from_company()` path on any problem — no key,
timeout, over budget, unparsable output. The endpoint never errors because of the AI and
never waits on it longer than 4 seconds. The browser also normalizes the field while the
visitor types ("Minha Empresa" becomes `minha-empresa` live, instead of a validation error).

## Where things live

- `ai_saas/saas/slug_ai.py` — the whole AI integration. `ai_slug_candidates(name)` returns
  validated candidates or `[]`; `_complete()` is the only function that touches the network
  (tests patch it). Model output is never trusted: every candidate is re-checked against
  `SLUG_RE` + `RESERVED_SLUGS` before it can be suggested.
- `ai_saas/saas/accounts.py` `_suggest_subdomain()` — tries AI candidates first, then the
  deterministic slug variants. Same signature, same callers.
- `ai_saas/www/registo/index.js` — `normalizeSlug()` + the input listener; the prefill also
  repairs a restored-but-invalid value the visitor never touched.

## Configuration (per site, never a fixture)

```bash
bench --site erp.mozeconomia.co.mz set-config anthropic_api_key sk-ant-...
```

Read via `frappe.conf.anthropic_api_key`. Sites without the key (dev, tests, the
registo-curati / registo-kalenyholding clones' sites) run deterministic-only — that is the
intended off switch.

**Identity-linked keys** (the newer Console key type, tied to your Console user) also need
the workspace the requests act in — the API refuses them with
"anthropic-workspace-id is required" otherwise:

```bash
bench --site <site> set-config anthropic_workspace_id wrkspc_...
```

The workspace ID is in console.anthropic.com → Settings → Workspaces. Classic
workspace/service API keys need no extra config.

## Cost controls

- One API call ≈ 600 input + 80 output tokens on Haiku 4.5 ($1/$5 per MTok) ≈ **$0.001**.
- Candidates are cached in Redis for 24 h per normalized company name (raw Redis keys
  `slug_ai:<sha1>`, deliberately not `get_value`/`set_value` — a get_value miss poisons the
  worker-local cache with None and would shadow the Redis entry). Availability is *not*
  cached — it is re-probed on every call, so a cached candidate can never be suggested
  while taken.
- Global wallet guard: `DAILY_CAP = 500` AI calls/day (Redis counter `slug_ai:daily`).
  Over the cap — or with Redis down — the AI silently steps aside and the deterministic
  path serves. Worst-case abuse ≈ $0.50/day.

## Dependency and deploy

`pyproject.toml` declares `anthropic~=1.0` (SDK 1.x, ships its own `httpx2`; no conflict
with Frappe's `httpx`). The `import anthropic` is inside `_complete()`, so the app still
boots if a deploy misses the pip step — the AI just logs and falls back.

Production deploy order:

```bash
git pull
bench setup requirements          # installs anthropic into the bench env
bench --site erp.mozeconomia.co.mz set-config anthropic_api_key sk-ant-...   # once
sudo supervisorctl restart all    # bench restart alone is not enough on prod
```

## Verifying

```bash
bench --site <site> run-tests --module ai_saas.tests.test_signup   # TestSlugAI, no network
bench --site <site> execute ai_saas.saas.slug_ai.ai_slug_candidates --args "['Farmácia Central, Lda']"
# run it twice — the second call must return instantly (cache hit, no second API call)
```

On /registo step 3: type `Minha Empresa, Lda` into the subdomain field and watch it
normalize live; clear the field and re-enter the step to see the AI prefill + "Disponível ✓".
