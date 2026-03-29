from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Iterable

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.requests import Request


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "chatbot.db"
OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_SYSTEM_PROMPT = (
    "You are a local offline AI assistant. Be helpful, accurate, and concise."
)


def ensure_database() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (conversation_id) REFERENCES conversations (id)
            )
            """
        )
        conn.commit()


def utc_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def first_title_from_prompt(prompt: str) -> str:
    compact = " ".join(prompt.split())
    return compact[:60] or "New chat"


def conversation_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "model": row["model"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def message_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "role": row["role"],
        "content": row["content"],
        "created_at": row["created_at"],
    }


class CreateConversationPayload(BaseModel):
    title: str | None = None
    model: str = "qwen2.5-coder:7b"


class MessagePayload(BaseModel):
    content: str = Field(min_length=1)
    model: str
    system_prompt: str | None = None


app = FastAPI(title="Offline Ollama Chatbot")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
async def startup_event() -> None:
    ensure_database()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "default_model": "qwen2.5-coder:7b",
            "default_system_prompt": DEFAULT_SYSTEM_PROMPT,
        },
    )


@app.get("/api/health")
async def health() -> JSONResponse:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            response.raise_for_status()
    except httpx.HTTPError as exc:
        return JSONResponse(
            {
                "status": "degraded",
                "detail": f"Could not reach Ollama at {OLLAMA_BASE_URL}: {exc}",
            },
            status_code=503,
        )

    return JSONResponse({"status": "ok"})


@app.get("/api/models")
async def list_models() -> JSONResponse:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{OLLAMA_BASE_URL}/api/tags")
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"Could not load models: {exc}") from exc

    payload = response.json()
    models = [item["name"] for item in payload.get("models", [])]
    return JSONResponse({"models": models})


@app.get("/api/conversations")
async def list_conversations() -> JSONResponse:
    with closing(get_db()) as conn:
        rows = conn.execute(
            """
            SELECT id, title, model, created_at, updated_at
            FROM conversations
            ORDER BY updated_at DESC
            """
        ).fetchall()

    return JSONResponse({"conversations": [conversation_to_dict(row) for row in rows]})


@app.post("/api/conversations")
async def create_conversation(payload: CreateConversationPayload) -> JSONResponse:
    conversation_id = str(uuid.uuid4())
    now = utc_now()
    title = payload.title or "New chat"

    with closing(get_db()) as conn:
        conn.execute(
            """
            INSERT INTO conversations (id, title, model, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (conversation_id, title, payload.model, now, now),
        )
        conn.commit()

    return JSONResponse(
        {
            "conversation": {
                "id": conversation_id,
                "title": title,
                "model": payload.model,
                "created_at": now,
                "updated_at": now,
            }
        }
    )


@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str) -> JSONResponse:
    with closing(get_db()) as conn:
        conversation = conn.execute(
            """
            SELECT id, title, model, created_at, updated_at
            FROM conversations
            WHERE id = ?
            """,
            (conversation_id,),
        ).fetchone()
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")

        messages = conn.execute(
            """
            SELECT id, role, content, created_at
            FROM messages
            WHERE conversation_id = ?
            ORDER BY id ASC
            """,
            (conversation_id,),
        ).fetchall()

    return JSONResponse(
        {
            "conversation": conversation_to_dict(conversation),
            "messages": [message_to_dict(row) for row in messages],
        }
    )


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str) -> JSONResponse:
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        deleted = conn.execute(
            "DELETE FROM conversations WHERE id = ?",
            (conversation_id,),
        )
        conn.commit()

    if deleted.rowcount == 0:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return JSONResponse({"deleted": True})


def load_messages(conn: sqlite3.Connection, conversation_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT role, content
        FROM messages
        WHERE conversation_id = ?
        ORDER BY id ASC
        """,
        (conversation_id,),
    ).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in rows]


def persist_message(
    conn: sqlite3.Connection,
    conversation_id: str,
    role: str,
    content: str,
) -> None:
    conn.execute(
        """
        INSERT INTO messages (conversation_id, role, content, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (conversation_id, role, content, utc_now()),
    )


def update_conversation_metadata(
    conn: sqlite3.Connection,
    conversation_id: str,
    content: str,
    model: str,
) -> None:
    existing = conn.execute(
        "SELECT title FROM conversations WHERE id = ?",
        (conversation_id,),
    ).fetchone()
    if existing is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    title = existing["title"]
    if title == "New chat":
        title = first_title_from_prompt(content)

    conn.execute(
        """
        UPDATE conversations
        SET title = ?, model = ?, updated_at = ?
        WHERE id = ?
        """,
        (title, model, utc_now(), conversation_id),
    )


def sse_event(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/api/conversations/{conversation_id}/messages")
async def send_message(conversation_id: str, payload: MessagePayload) -> StreamingResponse:
    with closing(get_db()) as conn:
        history = load_messages(conn, conversation_id)
        persist_message(conn, conversation_id, "user", payload.content)
        update_conversation_metadata(conn, conversation_id, payload.content, payload.model)
        conn.commit()

    async def stream_reply() -> Iterable[str]:
        assistant_chunks: list[str] = []
        messages = [{"role": "system", "content": payload.system_prompt or DEFAULT_SYSTEM_PROMPT}]
        messages.extend(history)
        messages.append({"role": "user", "content": payload.content})

        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST",
                    f"{OLLAMA_BASE_URL}/api/chat",
                    json={
                        "model": payload.model,
                        "messages": messages,
                        "stream": True,
                    },
                ) as response:
                    response.raise_for_status()

                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        chunk = json.loads(line)
                        if "message" in chunk:
                            content = chunk["message"].get("content", "")
                            if content:
                                assistant_chunks.append(content)
                                yield sse_event("token", {"content": content})
                        if chunk.get("done"):
                            break
        except httpx.HTTPError as exc:
            yield sse_event("error", {"detail": f"Ollama request failed: {exc}"})
            return

        full_reply = "".join(assistant_chunks).strip()
        if not full_reply:
            yield sse_event("error", {"detail": "The model returned an empty response."})
            return

        with closing(get_db()) as conn:
            persist_message(conn, conversation_id, "assistant", full_reply)
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (utc_now(), conversation_id),
            )
            conn.commit()

        yield sse_event("done", {"content": full_reply})

    return StreamingResponse(stream_reply(), media_type="text/event-stream")
