# app/pii_pseudo.py
import re
import spacy

# Regex für IBAN (recht streng)
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?\d{2,4}){3,7}\b")

# Regex für Adressen (sehr vereinfacht, reicht für Demos)
ADDRESS_RE = re.compile(r"\b([A-ZÄÖÜ][a-zäöüß]+(?:straße|str\.|weg|platz)\s+\d+,\s*\d{5}\s+[A-ZÄÖÜ][a-zäöüß]+)\b")

# spaCy-Model laden
try:
    nlp = spacy.load("de_core_news_sm")
except OSError:
    nlp = None
    print("[WARN] spaCy 'de_core_news_sm' nicht installiert. Installieren mit: python -m spacy download de_core_news_sm")

# Zähler für Pseudonyme
COUNTERS = {"PER": 0, "LOC": 0, "GPE": 0, "ORG": 0, "IBAN": 0, "ADDRESS": 0}

def _make_label(label: str) -> str:
    COUNTERS[label] += 1
    return f"[{label}_{COUNTERS[label]}]"

def pseudonymize(text: str):
    """Ersetzt erkannte PII durch Pseudonyme wie [PER_1], [IBAN_1]."""
    masked = text
    replacements = []

    # 1) IBANs
    for m in IBAN_RE.finditer(masked):
        label = _make_label("IBAN")
        replacements.append((m.start(), m.end(), label))
    masked = IBAN_RE.sub(lambda m: _make_label("IBAN"), masked)

    # 2) Adressen
    for m in ADDRESS_RE.finditer(masked):
        label = _make_label("ADDRESS")
        replacements.append((m.start(), m.end(), label))
    masked = ADDRESS_RE.sub(lambda m: _make_label("ADDRESS"), masked)

    # 3) spaCy NER
    if nlp:
        doc = nlp(masked)
        ents = []
        for ent in doc.ents:
            if ent.label_ in {"PER", "LOC", "GPE", "ORG"}:
                ents.append((ent.start_char, ent.end_char, ent.label_))
        # von hinten nach vorne ersetzen
        for start, end, label in sorted(ents, key=lambda x: -x[0]):
            repl = _make_label(label)
            masked = masked[:start] + repl + masked[end:]

    return masked, replacements
