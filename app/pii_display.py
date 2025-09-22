# app/pii_display.py

def replace_pseudonyms_with_masks(text: str) -> str:
    """
    Wandelt technische Pseudonyme wie [IBAN_1], [PER_2] in
    verständliche Masken für die Anzeige um.
    """
    text = text.replace("[PER_", "[Name maskiert ")
    text = text.replace("[LOC_", "[Ort maskiert ")
    text = text.replace("[ORG_", "[Organisation maskiert ")
    text = text.replace("[IBAN_", "[IBAN maskiert ")
    text = text.replace("[ADDRESS_", "[Adresse maskiert ")
    return text
