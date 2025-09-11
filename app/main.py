from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from typing import List, Dict
import os, re, json, datetime

from dotenv import load_dotenv
from openai import OpenAI

import asyncio, json
from fastapi.responses import StreamingResponse

# Lokale Imports
from .access_control import get_allowed_sources
from .pii_masking import mask_pii
from . import retriever

load_dotenv()  # lädt .env
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(APP_ROOT)
FRONTEND_INDEX = os.path.join(PROJECT_ROOT, "frontend", "index.html")
DOCS_DIR = os.path.join(PROJECT_ROOT, "data", "docs")
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

app = FastAPI(title="RAG Demo – Kredit Auszahlung", version="0.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class QueryRequest(BaseModel):
    question: str
    user_role: str

@app.get("/")
def serve_index():
    if os.path.exists(FRONTEND_INDEX):
        return FileResponse(FRONTEND_INDEX)
    return HTMLResponse("<h1>RAG Demo</h1><p>index.html fehlt.</p>")

@app.get("/api/health")
def health():
    return {"status": "ok"}

def write_audit_log(entry: Dict) -> None:
    ts = datetime.datetime.now().isoformat()
    entry = {"ts": ts, **entry}
    path = os.path.join(LOG_DIR, "audit.log")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def _sse_event(event: str, payload: dict) -> bytes:
    # SSE-Format: optional "event:" + "data:" + Leerzeile
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

def strip_markdown(text: str) -> str:
    """Entfernt einfache Markdown-Auszeichnungen wie **fett**, *kursiv*, _kursiv_."""
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)  # **fett**
    text = re.sub(r"\*(.*?)\*", r"\1", text)      # *kursiv*
    text = re.sub(r"_(.*?)_", r"\1", text)        # _kursiv_
    return text

def call_llm(question: str, contexts: List[str]) -> str:
    """Ruft OpenAI GPT auf, um Antwort aus Kontexten zu generieren."""
    prompt = f"""Beantworte die Nutzerfrage basierend auf den folgenden Dokument-Auszügen.
Wenn die Antwort nicht eindeutig ist, sage das klar.

Frage: {question}

Kontext:
{chr(10).join(contexts)}

Antwort (auf Deutsch):"""
    resp = client.chat.completions.create(
        model="gpt-4o-mini",  # oder gpt-4o
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    return resp.choices[0].message.content.strip()

@app.post("/api/query")
def query(req: QueryRequest):
    allowed = get_allowed_sources(req.user_role)
    pipeline = []

    # 1) Zugriffsprüfung
    if not allowed:
        pipeline.append({"step": "Zugriffsprüfung (ABAC)", 
                         "arch_layer": "Identity & Access / Policy-as-Code", 
                         "status": "blocked"})
        raise HTTPException(status_code=403, detail="Keine Dokumente für diese Rolle freigegeben.")
    else:
        pipeline.append({"step": "Zugriffsprüfung (ABAC)", 
                         "arch_layer": "Identity & Access / Policy-as-Code", 
                         "status": "done"})

    # 2) Retriever
    hits = retriever.search(req.question, allowed_sources=allowed, k=3)
    pipeline.append({"step": "Retriever + Vektordatenbank", 
                     "arch_layer": "Data Platform / MLOps", 
                     "status": "done"})

    # 3) LLM
    if hits:
        contexts = [h["text"] for h in hits]
        raw_answer = call_llm(req.question, contexts)
        pipeline.append({"step": "LLM (Generator)", 
                         "arch_layer": "MLOps / LLMOps", 
                         "status": "done"})
    else:
        raw_answer = "Keine Informationen gefunden."
        pipeline.append({"step": "LLM (Generator)", 
                         "arch_layer": "MLOps / LLMOps", 
                         "status": "skipped"})

    # 4) PII-Maskierung
    masked = mask_pii(raw_answer)
    pipeline.append({"step": "PII-Maskierung", 
                     "arch_layer": "Data Protection", 
                     "status": "done"})

    # 5) Audit-Log
    sources = [{
        "document": h["source"],
        "chunk_id": h["chunk_id"],
        "title": h.get("title") or "",
        "preview": (h["text"][:240] + "...") if len(h["text"]) > 240 else h["text"]
    } for h in hits]
    write_audit_log({
        "user_role": req.user_role,
        "question": req.question,
        "answer_masked": masked,
        "sources": sources
    })
    pipeline.append({"step": "Quellenangabe + Audit-Log", 
                     "arch_layer": "Observability & Audit", 
                     "status": "done"})

    return {"answer": masked, "sources": sources, "pipeline": pipeline}


@app.get("/api/query_stream")
async def query_stream(question: str, user_role: str):
    async def event_gen():
        # 1) ABAC
        allowed = get_allowed_sources(user_role)
        if not allowed:
            step_abac = {
                "step": "Zugriffsprüfung (ABAC)",
                "arch_layer": "Identity & Access / Policy-as-Code",
                "status": "blocked",
                "detail": f"Rolle '{user_role}' hat keinen Zugriff."
            }
            yield _sse_event("step", step_abac)
            yield _sse_event("final", {
                "answer": "Keine freigegebenen Dokumente für diese Rolle.",
                "sources": [],
                "pipeline": [step_abac]
            })
            return

        step_abac = {
            "step": "Zugriffsprüfung (ABAC)",
            "arch_layer": "Identity & Access / Policy-as-Code",
            "status": "done",
            "detail": f"Rolle '{user_role}' darf auf {', '.join(allowed)} zugreifen."
        }
        yield _sse_event("step", step_abac)
        await asyncio.sleep(0.2)

        # 2) Retriever
        hits = retriever.search(question, allowed_sources=allowed, k=3)
        step_ret = {
            "step": "Retriever + Vektordatenbank",
            "arch_layer": "Data Platform / MLOps",
            "status": "done",
            "detail": f"{len(hits)} relevante Abschnitte gefunden."
        }
        yield _sse_event("step", step_ret)
        await asyncio.sleep(0.5)

        # 3) LLM
        contexts = [h["text"] for h in hits] if hits else []
        raw_answer = call_llm(question, contexts) if contexts else "Keine Infos gefunden."
        step_llm = {
            "step": "LLM (Generator)",
            "arch_layer": "MLOps / LLMOps",
            "status": "done" if contexts else "skipped",
            "detail": "Antwort generiert." if contexts else "Übersprungen."
        }
        yield _sse_event("step", step_llm)
        await asyncio.sleep(0.5)

        # 4) PII
        masked = mask_pii(raw_answer)
        masked_clean = strip_markdown(masked)
        step_pii = {
            "step": "PII-Maskierung",
            "arch_layer": "Data Protection",
            "status": "done",
            "detail": "Personenbezogene Daten maskiert."
        }
        yield _sse_event("step", step_pii)
        await asyncio.sleep(0.5)

        # 5) Audit
        sources = [{
            "document": h["source"],
            "chunk_id": h["chunk_id"],
            "title": h.get("title") or "",
        } for h in hits]
        write_audit_log({
            "user_role": user_role,
            "question": question,
            "answer_masked": masked_clean,
            "sources": sources
        })
        step_audit = {
            "step": "Quellenangabe + Audit-Log",
            "arch_layer": "Observability & Audit",
            "status": "done",
            "detail": f"{len(sources)} Quelle(n) geloggt."
        }
        yield _sse_event("step", step_audit)
        await asyncio.sleep(0.2)

        # Final
        yield _sse_event("final", {
            "answer": masked_clean,
            "sources": sources
        })

    return StreamingResponse(event_gen(), media_type="text/event-stream")

