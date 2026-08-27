"""Parse a Mozambican postal address written as one line into ERPNext Address fields.

People write addresses the way they say them: street and number, then the bairro,
then the city — sometimes the province — separated by commas:

    Av. 25 de Setembro, 1234, Bairro Central, Maputo
    Rua da Resistência, Q. 12, Casa 5, Bairro Munhava, Beira
    Estrada Nacional 1, Matola
    Rua 3, Chimoio, Manica

The last comma-separated part is the city (or the province, when the part before it
is a known city); a part starting with "Bairro" / "B." is the neighbourhood; everything
before that is the street line. Nothing is ever lost: whatever does not parse stays
in address_line1.
"""

import re
import unicodedata

# city -> province (ERPNext Address.state). Provincial capitals and the larger towns.
CITY_PROVINCE = {
	"Maputo": "Maputo Cidade",
	"Matola": "Maputo",
	"Boane": "Maputo",
	"Marracuene": "Maputo",
	"Namaacha": "Maputo",
	"Xai-Xai": "Gaza",
	"Chókwè": "Gaza",
	"Chokwe": "Gaza",
	"Inhambane": "Inhambane",
	"Maxixe": "Inhambane",
	"Vilankulo": "Inhambane",
	"Beira": "Sofala",
	"Dondo": "Sofala",
	"Chimoio": "Manica",
	"Manica": "Manica",
	"Tete": "Tete",
	"Moatize": "Tete",
	"Quelimane": "Zambézia",
	"Mocuba": "Zambézia",
	"Gurué": "Zambézia",
	"Nampula": "Nampula",
	"Nacala": "Nampula",
	"Angoche": "Nampula",
	"Ilha de Moçambique": "Nampula",
	"Pemba": "Cabo Delgado",
	"Montepuez": "Cabo Delgado",
	"Lichinga": "Niassa",
	"Cuamba": "Niassa",
}
PROVINCES = {
	"Maputo Cidade", "Cidade de Maputo", "Maputo Província", "Província de Maputo", "Maputo",
	"Gaza", "Inhambane", "Sofala", "Manica", "Tete", "Zambézia", "Zambezia", "Nampula",
	"Cabo Delgado", "Niassa",
}
_BAIRRO = re.compile(r"^(bairro|b\.|br\.)\s+", re.I)
# "Cidade de Maputo", "Município da Matola", "Distrito de Boane" — administrative
# wrapping people write around a city name.
_ADMIN_PREFIX = re.compile(
	r"^(cidade|municipio|município|distrito|vila|provincia|província)\s+(de|da|do|d')?\s*", re.I
)
# Spellings that are not the canonical one but mean the same place.
CITY_ALIASES = {
	"xai xai": "Xai-Xai",
	"xaixai": "Xai-Xai",
	"vilanculos": "Vilankulo",
	"vilankulos": "Vilankulo",
	"vilanculo": "Vilankulo",
	"ilha de mocambique": "Ilha de Moçambique",
	"nacala porto": "Nacala",
	"joao belo": "Xai-Xai",
	"lourenco marques": "Maputo",
}


def _fold(part: str) -> str:
	"""Lowercase, unaccented, punctuation-free — how a name is compared, never stored."""
	txt = unicodedata.normalize("NFKD", part or "")
	txt = "".join(c for c in txt if not unicodedata.combining(c))
	return re.sub(r"\s+", " ", re.sub(r"[^\w\s-]", " ", txt)).strip().lower()


def _norm(part: str) -> str:
	return re.sub(r"\s+", " ", part).strip(" .,;")


def canonical_city(part: str):
	"""The canonical spelling of a city we know, or None. Tolerates case, accents and
	the administrative wrapper ("Cidade de Maputo" is Maputo)."""
	folded = _fold(_ADMIN_PREFIX.sub("", _norm(part or "")))
	if not folded:
		return None
	if folded in CITY_ALIASES:
		return CITY_ALIASES[folded]
	for city in CITY_PROVINCE:
		if folded == _fold(city):
			return city
	return None


def _known_city(part: str):
	return canonical_city(part)


# Longest first, so "Ilha de Moçambique" wins over any shorter name inside it.
_CITY_PATTERNS = sorted(
	[(city, _fold(city)) for city in CITY_PROVINCE] + [(city, alias) for alias, city in CITY_ALIASES.items()],
	key=lambda pair: len(pair[1]), reverse=True,
)


def find_city_in_text(text: str):
	"""The city named anywhere in a line written without commas — "Av. 25 de Setembro
	Maputo", "Rua 3 Beira". Returns (canonical city, the text with that name removed)
	or (None, text)."""
	folded = _fold(text)
	if not folded:
		return None, text
	for city, needle in _CITY_PATTERNS:
		m = re.search(r"(?<![\w-])" + re.escape(needle) + r"(?![\w-])", folded)
		if not m:
			continue
		# Map the match back onto the original text word-wise: fold each word and drop
		# the run that produced the match, so accents and punctuation survive elsewhere.
		words = _norm(text).split()
		span = len(needle.split())
		for i in range(len(words) - span + 1):
			if _fold(" ".join(words[i:i + span])) == needle:
				rest = _norm(" ".join(words[:i] + words[i + span:]))
				return city, rest
		return city, text
	return None, text


def parse_mz_address(text: str) -> dict:
	"""Return {"address_line1", "address_line2", "city", "state"} from one free-text line."""
	parts = [p for p in (_norm(p) for p in (text or "").split(",")) if p]
	out = {"address_line1": "", "address_line2": "", "city": "", "state": ""}
	if not parts:
		return out

	# Province at the very end, after a known city: "Rua 3, Chimoio, Manica".
	if len(parts) >= 2 and parts[-1] in PROVINCES and _known_city(parts[-2]):
		out["state"] = parts.pop()
	# The last part is the city — normalised to its known spelling when we have it.
	if len(parts) > 1 and not _BAIRRO.match(parts[-1]):
		city = parts.pop()          # the last part is the city — even one we do not list
	elif len(parts) == 1 and _known_city(parts[0]):
		city = parts.pop()          # the whole address was a city name
	else:
		city = ""                   # a bairro is not a city: ask rather than guess
	if city:
		known = _known_city(city)
		out["city"] = known or city
		out["state"] = out["state"] or CITY_PROVINCE.get(known or "", "")

	# The bairro, wherever it was written.
	rest = []
	for p in parts:
		if _BAIRRO.match(p) and not out["address_line2"]:
			out["address_line2"] = p
		else:
			rest.append(p)
	out["address_line1"] = ", ".join(rest)

	# No commas, or none of the parts was a city we know: look for a city name inside
	# the line itself ("Av. 25 de Setembro Maputo", "Cidade de Maputo").
	for key in ("address_line1", "address_line2"):
		if out["city"] or not out[key]:
			continue
		found, rest_text = find_city_in_text(out[key])
		if found:
			out["city"] = found
			out["state"] = out["state"] or CITY_PROVINCE.get(found, "")
			out[key] = rest_text
	# address_line1 is mandatory on Address, so it is never left empty while anything
	# else was typed: the bairro moves up, or failing that the city itself.
	if not out["address_line1"]:
		out["address_line1"] = out["address_line2"] or out["city"]
		if out["address_line1"] and out["address_line1"] == out["address_line2"]:
			out["address_line2"] = ""
	return out
