from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Dict
import os, re, json, datetime, asyncio

from dotenv import load_dotenv
from openai import OpenAI

# Lokale Imports
from .access_control import get_allowed_sources
from .pii_pseudo import pseudonymize  # Vor LLM
from .pii_display import replace_pseudonyms_with_masks  # Nach LLM
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

app = FastAPI(title="RAG Demo – Kredit Auszahlung", version="0.4.0")

# Statische Auslieferung der Testdokumente
app.mount("/docs", StaticFiles(directory=DOCS_DIR), name="docs")

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
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def strip_markdown(text: str) -> str:
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\*(.*?)\*", r"\1", text)
    text = re.sub(r"_(.*?)_", r"\1", text)
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
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )
    return resp.choices[0].message.content.strip()


@app.post("/api/query")
def query(req: QueryRequest, mode: str = "default"):
    allowed = get_allowed_sources(req.user_role)
    pipeline = []

    # 1) ABAC
    if not allowed:
        raise HTTPException(status_code=403, detail="Keine Dokumente für diese Rolle freigegeben.")
    pipeline.append({"step": "Zugriffsprüfung (ABAC)", "status": "done"})

    # 2) Retriever
    hits = retriever.search(req.question, allowed_sources=allowed, k=3)
    pipeline.append({"step": "Retriever + Vektordatenbank", "status": "done"})

    if mode == "mask_only":
        # Keine Pseudonymisierung vor LLM
        contexts = [h["text"] for h in hits] if hits else []
        pipeline.append({"step": "PII-Pseudonymisierung (vor LLM)", "status": "skipped"})
    else:
        # Mit Pseudonymisierung
        contexts = [h["text"] for h in hits] if hits else []
        pseudo_contexts = []
        for c in contexts:
            c_pseudo, _ = pseudonymize(c)
            pseudo_contexts.append(c_pseudo)
        contexts = pseudo_contexts
        pipeline.append({"step": "PII-Pseudonymisierung (vor LLM)", "status": "done"})

    # LLM
    raw_answer = call_llm(req.question, contexts) if contexts else "Keine Infos gefunden."
    pipeline.append({"step": "LLM (Generator)", "status": "done"})

    # Maskierung nach LLM
    final_answer = replace_pseudonyms_with_masks(raw_answer)
    pipeline.append({"step": "PII-Maskierung (nach LLM)", "status": "done"})

    return {"answer": final_answer, "sources": hits, "pipeline": pipeline}


@app.get("/api/query_stream")
async def query_stream(question: str, user_role: str, mode: str = "default"):
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
        contexts = [h["text"] for h in hits] if hits else []
        step_ret = {
            "step": "Retriever + Vektordatenbank",
            "arch_layer": "Data Platform / MLOps",
            "status": "done",
            "detail": f"{len(hits)} relevante Abschnitte gefunden.",
            "snippets": [
                {
                    "source": h["source"],
                    "chunk_id": h["chunk_id"],
                    "preview": h["text"][:180] + ("..." if len(h["text"]) > 180 else "")
                }
                for h in hits
            ],
        }
        yield _sse_event("step", step_ret)
        await asyncio.sleep(0.3)

        # 3) Pseudonymisierung (nur im Default-Modus)
        if mode == "pseudonymize":
            pseudo_contexts = []
            for c in contexts:
                c_pseudo, _ = pseudonymize(c)
                pseudo_contexts.append(c_pseudo)

            step_pseudo = {
                "step": "PII-Pseudonymisierung (vor LLM)",
                "arch_layer": "Data Protection",
                "status": "done",
                "detail": f"{len(pseudo_contexts)} Abschnitte pseudonymisiert.",
                "extra": "\n\n".join(pseudo_contexts)
            }
            yield _sse_event("step", step_pseudo)
            await asyncio.sleep(0.3)

            llm_contexts = pseudo_contexts
        else:
            # Nur Maskierung: unveränderte Kontexte direkt an LLM
            llm_contexts = contexts

        # 4) LLM
        raw_answer = call_llm(question, llm_contexts) if llm_contexts else "Keine Infos gefunden."
        step_llm = {
            "step": "LLM (Generator)",
            "arch_layer": "MLOps / LLMOps",
            "status": "done" if llm_contexts else "skipped",
            "detail": "Antwort generiert." if llm_contexts else "Übersprungen."
        }
        yield _sse_event("step", step_llm)
        await asyncio.sleep(0.3)

        # 5) Maskierung nach LLM
        masked = mask_pii(raw_answer)
        masked_clean = strip_markdown(masked)

        # Anzahl Maskierungen zählen
        num_masks = len(re.findall(r"\[.*?maskiert.*?\]", masked_clean, flags=re.IGNORECASE))

        if num_masks > 0:
            detail_text = f"Antwortausgabe maskiert ({num_masks} Stelle(n) ersetzt)."
        else:
            detail_text = "Antwortausgabe maskiert."

        step_mask = {
            "step": "PII-Maskierung (nach LLM)",
            "arch_layer": "Data Protection",
            "status": "done",
            "detail": detail_text
        }
        yield _sse_event("step", step_mask)
        await asyncio.sleep(0.3)

        # 6) Audit
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

