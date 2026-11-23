"""Text normalisation used for matching. Pure functions, no I/O."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

# --- street addresses --------------------------------------------------------
_STREET_WORDS = {
    "street": "st", "avenue": "ave", "av": "ave", "road": "rd", "drive": "dr",
    "lane": "ln", "boulevard": "blvd", "pike": "pk", "court": "ct", "place": "pl",
    "highway": "hwy", "parkway": "pkwy", "circle": "cir", "terrace": "ter",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
    "saint": "st",
}
_PO_BOX = re.compile(r"^\s*p\.?\s*o\.?\s*box\b", re.I)


def street_key(street: str) -> str:
    """'980 West Michigan Avenue' and '980 W Michigan Ave' -> '980 w michigan ave'.

    Returns '' when there is no usable physical street (blank or a PO Box).
    """
    if not street or _PO_BOX.match(street):
        return ""
    tokens = re.sub(r"[^a-z0-9 ]", " ", street.lower()).split()
    return " ".join(_STREET_WORDS.get(t, t) for t in tokens)


def is_po_box(street: str) -> bool:
    return bool(street and _PO_BOX.match(street))


def street_relation(a: str, b: str) -> str:
    """'match', 'mismatch', or 'unknown' (one side has no physical street)."""
    ka, kb = street_key(a), street_key(b)
    if not ka or not kb:
        return "unknown"
    return "match" if ka == kb else "mismatch"


# --- names -------------------------------------------------------------------
_NAME_STOPWORDS = {"the", "of", "at", "and", "in"}
_NAME_SYNONYMS = {
    "rehabilitation": "rehab", "centre": "center", "ctr": "center",
    "healthcare": "health care", "sr": "senior",
}
# Words that say what kind of place it is rather than which place it is.
_GENERIC_WORDS = {
    "senior", "living", "care", "center", "health", "nursing", "rehab", "community",
    "communities", "home", "retirement", "campus", "assisted", "memory",
}


def name_tokens(name: str) -> list:
    text = (name or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]", " ", text)
    tokens = []
    for tok in text.split():
        tok = _NAME_SYNONYMS.get(tok, tok)
        tokens.extend(t for t in tok.split() if t not in _NAME_STOPWORDS)
    return tokens


def same_name(a: str, b: str) -> bool:
    """True when two names differ only cosmetically.

    'The Arbors at Bellhaven - Dayton' == 'Arbors at Bellhaven Dayton'
    'Bellhaven Rehabilitation & Nursing of Grove City' == 'Bellhaven Rehab and Nursing of Grove City'
    """
    ta, tb = name_tokens(a), name_tokens(b)
    return bool(ta) and ta == tb


def name_similarity(a: str, b: str, brand: str = "") -> float:
    """0..1 similarity on the distinctive part of the name (brand + generic words removed).

    Only used to surface look-alikes as evidence; decisions use same_name().
    """
    if same_name(a, b):
        return 1.0
    brand_tokens = set(name_tokens(brand))

    def distinctive(name):
        toks = [t for t in name_tokens(name) if t not in brand_tokens and t not in _GENERIC_WORDS]
        return " ".join(toks)

    da, db = distinctive(a), distinctive(b)
    if not da or not db:
        return 0.0
    return round(SequenceMatcher(None, da, db).ratio(), 2)


# --- small helpers -----------------------------------------------------------
def digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def same_phone(a: str, b: str) -> bool:
    da, db = digits(a), digits(b)
    return len(da) >= 10 and da[-10:] == db[-10:]


def norm_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (value or "").lower())).strip()


def zip_is_typo_of(a: str, b: str) -> bool:
    """Two different 5-digit zips that are one slip apart (swapped or one wrong digit)."""
    a, b = digits(a)[:5], digits(b)[:5]
    if len(a) != 5 or len(b) != 5 or a == b:
        return False
    diff = [i for i in range(5) if a[i] != b[i]]
    if len(diff) == 1:
        return True
    return len(diff) == 2 and sorted(a) == sorted(b)


def money(value) -> str:
    try:
        return "${:,.0f}".format(float(value or 0))
    except (TypeError, ValueError):
        return str(value)
