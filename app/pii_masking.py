import os
import re
import spacy

# Regex für IBAN (mit/ohne Leerzeichen)
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?\d{2,4}){3,7}\b")

# False Positives – diese Wörter nicht maskieren
FALSE_POSITIVES = {
    "Auszahlung", "Auszahlungen",
    "Überweisung", "Überweisungen",
    "Laufzeit", "Laufzeiten",
    "Kontoverbindung", "Kontoverbindungen",
    "Bankarbeitstag", "Bankarbeitstagen",
    "IBAN",
    "Stunden"
}

# Länder/Städte, die nicht als PII gelten
NON_SENSITIVE_LOCATIONS = {
    "Deutschland", "Frankreich", "Österreich", "Schweiz", "München", "Berlin", "Paris"
}

# spaCy-Modell laden
try:
    nlp = spacy.load("de_core_news_sm")
except OSError:
    nlp = None
    print("[WARN] spaCy-Modell 'de_core_news_sm' fehlt. Installieren mit: python -m spacy download de_core_news_sm")

# Nur diese Entitäten maskieren
MASK_LABELS = {"PER", "LOC", "GPE", "ORG", "ADDRESS"}


def _looks_like_time_or_quantity(text: str) -> bool:
    """Erkennt Zeitangaben wie '24–48 Stunden' oder '2 Tage'."""
    if any(ch.isdigit() for ch in text):
        if any(unit in text.lower() for unit in ["stunde", "stunden", "tag", "tage", "wochen", "monat", "monate"]):
            return True
    return False


def mask_pii(text: str) -> str:
    # IBANs ersetzen
    masked = IBAN_RE.sub("[IBAN maskiert]", text)

    if nlp:
        doc = nlp(masked)
        entities = [(ent.start_char, ent.end_char, ent.label_, ent.text) for ent in doc.ents]

        for start, end, label, ent_text in sorted(entities, key=lambda x: -x[0]):
            if label not in MASK_LABELS:
                continue
            if ent_text in FALSE_POSITIVES:
                continue
            if _looks_like_time_or_quantity(ent_text):
                continue
            if label in {"LOC", "GPE"} and ent_text in NON_SENSITIVE_LOCATIONS:
                continue

            # Ersetzungen
            if label == "PER":
                repl = "[Name maskiert]"
            elif label in {"LOC", "GPE"}:
                repl = "[Ort maskiert]"
            elif label == "ORG":
                repl = "[Organisation maskiert]"
            else:  # ADDRESS
                repl = "[Adresse maskiert]"

            masked = masked[:start] + repl + masked[end:]

    return masked
