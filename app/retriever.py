# app/retriever.py
import os, json, re
from typing import List, Dict, Tuple
from pathlib import Path

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

DOCS_DIR = Path("data/docs")
INDEX_DIR = Path("data/index")
INDEX_FILE = INDEX_DIR / "faiss.index"
MAPPING_FILE = INDEX_DIR / "mapping.json"

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
_model = None
_index = None
_mapping = None

# Schlüsselwörter für Dauer/Prozess nach Bewilligung
BOOST_KWS = [
    r"\bbewilligung\b",
    r"\bauszahlung\b",
    r"\binnen\b|\binnerhalb\b|\bnach\b",
    r"\btag\b|\btage\b|\bbankarbeitstag\b|\bbankarbeitstagen\b",
    r"\büberweisung\b|\bkontoverbindung\b"
]
BOOST_RE = re.compile("|".join(BOOST_KWS), flags=re.IGNORECASE)


def get_model():
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def _read_markdown_chunks(text: str) -> List[Tuple[str, str]]:
    """
    Schlaues Chunking:
      - Erkenne Markdown-Überschriften (#, ##, ###)
      - Chunk = heading + nachfolgender Absatzblock
    Fällt zurück auf Doppel-NEWLINE-Split, wenn keine Headings vorhanden.
    Gibt Liste von (title, body)-Tupeln zurück (title kann leer sein).
    """
    lines = text.splitlines()
    chunks: List[Tuple[str, str]] = []
    current_title = None
    buffer: List[str] = []

    def flush_buffer(title, buf):
        body = "\n".join(buf).strip()
        if body:
            chunks.append((title or "", body))

    for ln in lines:
        if re.match(r"^\s*#{1,6}\s+", ln):  # Überschrift
            # alten Buffer wegschreiben
            flush_buffer(current_title, buffer)
            buffer = []
            # neue Überschrift aufnehmen
            current_title = re.sub(r"^\s*#{1,6}\s+", "", ln).strip()
        else:
            buffer.append(ln)

    flush_buffer(current_title, buffer)

    # Falls nichts erkannt: simpler Absatzsplit
    if not chunks:
        paras = [p.strip() for p in re.split(r"\n\n+", text) if p.strip()]
        chunks = [("", p) for p in paras]

    # Kombiniere title + body als endgültigen Chunk-Text
    final_chunks: List[Tuple[str, str]] = []
    for title, body in chunks:
        if title:
            final_chunks.append((title, f"{title}\n{body}".strip()))
        else:
            final_chunks.append(("", body))
    return final_chunks


def ingest_docs() -> None:
    """Lädt alle .md/.txt, erzeugt Embeddings und speichert FAISS-Index + Mapping."""
    INDEX_DIR.mkdir(parents=True, exist_ok=True)

    docs: List[str] = []
    mapping: List[Dict] = []

    for fname in os.listdir(DOCS_DIR):
        if not fname.lower().endswith((".md", ".txt")):
            continue
        path = DOCS_DIR / fname
        text = path.read_text(encoding="utf-8")
        # besseres Chunking:
        md_chunks = _read_markdown_chunks(text)

        # optional: kleine Slide-Window-Redundanz (überlappend) – hier nicht nötig
        for i, (title, chunk_text) in enumerate(md_chunks):
            docs.append(chunk_text)
            mapping.append({
                "source": fname,
                "chunk_id": i,
                "title": title,
                "text": chunk_text
            })

    if not docs:
        raise RuntimeError(f"Keine Dokumente unter {DOCS_DIR} gefunden.")

    model = get_model()
    embeddings = model.encode(docs, convert_to_numpy=True, show_progress_bar=True)
    # Cosine-Similarity via L2-Norm-Normalize + inner product
    faiss.normalize_L2(embeddings)

    dim = embeddings.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(embeddings)

    faiss.write_index(index, str(INDEX_FILE))
    MAPPING_FILE.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[INFO] Index gespeichert: {INDEX_FILE}, Mapping: {MAPPING_FILE}")


def load_index():
    global _index, _mapping
    if _index is None and INDEX_FILE.exists():
        _index = faiss.read_index(str(INDEX_FILE))
        _mapping = json.loads(MAPPING_FILE.read_text(encoding="utf-8"))
    return _index, _mapping


def _keyword_boost(text: str) -> float:
    """Einfacher Keyword-Boost. Liefert Bonus zwischen 0.0 und 0.2 je nach Trefferanzahl."""
    if not text:
        return 0.0
    hits = len(BOOST_RE.findall(text))
    # saturiere den Boost; z.B. max 0.2
    return min(0.04 * hits, 0.2)


def search(query: str, allowed_sources: List[str], k: int = 3) -> List[Dict]:
    """
    Semantische Suche (FAISS) + Keyword-Boost.
    Wir holen erst mehr Kandidaten (K'=8) und reranken lokal.
    """
    index, mapping = load_index()
    if index is None:
        return []

    model = get_model()
    q_emb = model.encode([query], convert_to_numpy=True)
    faiss.normalize_L2(q_emb)

    K_PRIME = max(k * 2 + 2, 8)  # hole mehr Kandidaten
    D, I = index.search(q_emb, K_PRIME)

    cands: List[Dict] = []
    for idx, score in zip(I[0], D[0]):
        if idx == -1:
            continue
        hit = mapping[idx]
        if allowed_sources and hit["source"] not in allowed_sources:
            continue
        text = hit["text"]
        bonus = _keyword_boost(text)
        hybrid = float(score) + bonus
        cands.append({
            "source": hit["source"],
            "chunk_id": hit["chunk_id"],
            "title": hit.get("title"),
            "text": text,
            "score_cosine": float(score),
            "score_hybrid": hybrid
        })

    # Rerank nach hybrid score
    cands.sort(key=lambda x: x["score_hybrid"], reverse=True)
    return cands[:k]


if __name__ == "__main__":
    ingest_docs()
