from fastapi import Depends, FastAPI
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.db import get_session
from app.models import ChatMessage
from services.rag import answer

HANDOFF = "Передаю ваш вопрос менеджеру."


class ChatIn(BaseModel):
    session_id: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ChatOut(BaseModel):
    answer: str
    escalate: bool
    sources: list[int]


app = FastAPI()


def handle_message(session: Session, session_id: str, message: str) -> ChatOut:
    rows = session.exec(
        select(ChatMessage)
        .where(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.created_at, ChatMessage.id)
    ).all()
    history = [{"role": row.role, "content": row.text} for row in rows]
    result = answer(message, history, session)
    escalate = bool(result.get("escalate"))
    if escalate:
        reply = HANDOFF
        sources: list[int] = []
    else:
        reply = str(result.get("answer") or "")
        sources = [int(item_id) for item_id in result.get("sources") or []]
    session.add(ChatMessage(session_id=session_id, role="user", text=message, escalated=False))
    session.add(
        ChatMessage(session_id=session_id, role="assistant", text=reply, escalated=escalate)
    )
    session.commit()
    return ChatOut(answer=reply, escalate=escalate, sources=sources)


@app.post("/chat", response_model=ChatOut)
def chat(body: ChatIn, session: Session = Depends(get_session)) -> ChatOut:
    return handle_message(session, body.session_id, body.message)
