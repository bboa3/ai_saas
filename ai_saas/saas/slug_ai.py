"""AI slug suggestions for /registo — Claude Haiku 4.5 behind a cache and a daily budget.

`ai_slug_candidates` never raises to the caller: any problem (no key, timeout, bad model
output, budget spent) means an empty list, and accounts._suggest_subdomain falls back to
the deterministic slug_from_company path. The API key is per-site configuration, never a
fixture: `bench --site <site> set-config anthropic_api_key sk-ant-...`.
"""

import hashlib
import json

import frappe

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 300
REQUEST_TIMEOUT = 4.0  # seconds — the suggest endpoint must stay interactive
CACHE_TTL = 24 * 3600  # candidates only; availability is re-probed on every call
DAILY_CAP = 500  # global AI calls/day — bounds worst-case abuse at ~$0.50/day

# The good/bad examples below are real slugs from the 2026-08-30 tenant inventory —
# the definition of "good" is what actual Mozambican businesses chose for themselves.
_SYSTEM = """You suggest account subdomains for an invoicing and management (ERP) system in
Mozambique, from the company's name (Portuguese/Mozambican context). The slug is what a
business owner answers out loud to "qual é o endereço do vosso sistema?" — it must be
instantly recognizable as THEIR business and easy to say and type.

Respond ONLY with a JSON array of 3 to 5 strings — no explanations, no markdown, no text
outside the array. Order the candidates best-first.

What makes a good slug (patterns from real tenants of this system):
1. The distinctive brand word alone is the best slug: "Sanlo Moçambique, LDA" -> "sanlo";
   "Papelaria Ndolene, LDA" -> "ndolene". Drop category, legal-form, geography and filler
   words (papelaria, serviços, comercial, lda, limitada, s.a., sarl, unipessoal, sociedade,
   companhia, empresa, grupo, holding, moçambique...) — keep a category word only when it
   carries the identity ("Farmácia Central, Lda" -> "farmacia-central").
2. Two short brand words: join them ("Mais Forte" -> "maisforte"; "Tecno Biza" ->
   "tecnobiza"); hyphenate only when joining hurts readability ("Bom Apetite" ->
   "bom-apetite"). At most one hyphen.
3. Initials are a strong candidate when the company is known by them — the name contains
   or naturally forms an acronym: "Automatic Fire Control, SU, LDA" -> "afc";
   "Oficinas Manutenção Preventiva - OMP, LDA" -> "omp". Minimum 3 letters.
4. Never invent: no truncations ("farmacia-ctr" is wrong), no reordering
   ("central-farmacia" is wrong), no made-up prefixes ("fc-farmacia" is wrong).
   Digits only when they are part of the name.
5. Short: ideally 4 to 14 characters; hard limits 3 to 40; only lowercase a-z, digits and
   hyphens; no accents; must not start or end with a hyphen.
6. When a city is given, exactly one candidate — the last — may append it as a
   distinguisher: "farmacia-central-matola". Never put the city in the other candidates."""


def ai_slug_candidates(company_name: str, city: str | None = None) -> list[str]:
	"""Best-first, validated slug candidates for a company name, or [] when the AI
	cannot help. The city (when known) lets the last candidate carry a city suffix —
	the collision distinguisher. Cached for a day per (name, city) so step-3
	back-and-forth, reloads and the next-day resume link cost one API call."""
	name = " ".join((company_name or "").split())
	city = " ".join((city or "").split()) or None
	if len(name) < 2:
		return []
	# Tests must never bill the API or depend on model output: the suite runs on a site
	# that has the real key. TestSlugAI opts back in (with _complete patched) via the flag.
	if frappe.flags.in_test and not getattr(frappe.flags, "slug_ai_test", False):
		return []
	key = _cache_key(name, city)
	cached = _cache_get(key)
	if cached is not None:
		try:
			return _validated(json.loads(cached))
		except Exception:
			pass
	if not frappe.conf.get("anthropic_api_key"):
		return []
	if not _budget_ok():
		return []
	try:
		raw = _complete(name, city)
	except Exception:
		# Timeouts and API errors are transient: log, don't cache, fall back.
		frappe.log_error(title="slug_ai: Anthropic call failed", message=frappe.get_traceback())
		return []
	candidates = _parse(raw)
	# Cache even an empty parse — a name the model can't slug shouldn't re-bill.
	_cache_set(key, json.dumps(candidates))
	return _validated(candidates)


def _complete(company_name: str, city: str | None = None) -> str:
	"""The one function that touches the network — the seam tests patch. The import
	lives here so the app still boots on a deploy that missed the pip step."""
	import anthropic

	# Identity-linked Console keys must name the workspace they act in; classic
	# workspace keys need no header. Optional: set-config anthropic_workspace_id wrkspc_...
	workspace_id = frappe.conf.get("anthropic_workspace_id")
	client = anthropic.Anthropic(
		api_key=frappe.conf.get("anthropic_api_key"),
		timeout=REQUEST_TIMEOUT,
		max_retries=0,  # a slow API must not stack 3 timeouts in front of the fallback
		default_headers={"anthropic-workspace-id": workspace_id} if workspace_id else None,
	)
	content = f"Company name: {company_name[:120]}"
	if city:
		content += f"\nCity: {city[:60]}"
	response = client.messages.create(
		model=MODEL,
		max_tokens=MAX_TOKENS,
		system=_SYSTEM,
		messages=[{"role": "user", "content": content}],
	)
	return "".join(block.text for block in response.content if block.type == "text")


def _parse(raw: str) -> list[str]:
	"""The JSON array out of whatever the model said, or []. Never trusts shape."""
	start, end = (raw or "").find("["), (raw or "").rfind("]")
	if start == -1 or end <= start:
		return []
	try:
		items = json.loads(raw[start : end + 1])
	except Exception:
		return []
	if not isinstance(items, list):
		return []
	return [item.strip().lower() for item in items if isinstance(item, str)]


def _validated(candidates: list) -> list[str]:
	"""Only candidates that pass OUR rules survive — model output is never trusted."""
	from ai_saas.saas.provisioning import RESERVED_SLUGS, SLUG_RE

	seen, out = set(), []
	for candidate in candidates:
		if not isinstance(candidate, str):
			continue
		if candidate in seen or candidate in RESERVED_SLUGS or not SLUG_RE.match(candidate):
			continue
		seen.add(candidate)
		out.append(candidate)
		if len(out) == 5:
			break
	return out


def _cache_get(key: str) -> str | None:
	"""Raw Redis read (prefixed key). Not get_value: a get_value miss poisons
	frappe.local.cache with None, which then shadows a later set_value(expires_in_sec=…)
	that writes only to Redis — the cache would never hit within a worker."""
	try:
		cache = frappe.cache()
		raw = cache.get(cache.make_key(key))
	except Exception:
		return None
	if raw is None:
		return None
	return raw.decode() if isinstance(raw, bytes) else str(raw)


def _cache_set(key: str, value: str) -> None:
	try:
		cache = frappe.cache()
		cache.set(cache.make_key(key), value, ex=CACHE_TTL)
	except Exception:
		pass  # Redis down: nothing cached, next call re-tries — same graceful path as _budget_ok


def _budget_ok() -> bool:
	"""Global daily wallet guard (same incr/expire pattern as signup._limit, but it
	never throws — over budget just means the deterministic path serves)."""
	try:
		cache = frappe.cache()
		count = cache.incr("slug_ai:daily")
		if count == 1:
			cache.expire("slug_ai:daily", 24 * 3600)
		return count <= DAILY_CAP
	except Exception:
		return False


def _cache_key(company_name: str, city: str | None = None) -> str:
	digest = hashlib.sha1(f"{company_name.lower()}|{(city or '').lower()}".encode()).hexdigest()
	return f"slug_ai:{digest}"
