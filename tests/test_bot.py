from contextlib import contextmanager

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import get_session
from app.models import ChatMessage
from bot.api import app

SESSION = "web-1"


@contextmanager
def _client(session):
    def override():
        yield session

    app.dependency_overrides[get_session] = override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def test_chat_returns_answer(session, monkeypatch):
    def fake_answer(question, history, db, client=None):
        assert question == "чем стабилизирует"
        assert history == []
        return {"answer": "Гироплатформой.", "sources": [4], "escalate": False}

    monkeypatch.setattr("bot.api.answer", fake_answer)
    with _client(session) as client:
        response = client.post(
            "/chat",
            json={"session_id": SESSION, "message": "чем стабилизирует"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "answer": "Гироплатформой.",
        "escalate": False,
        "sources": [4],
    }


def test_chat_escalation(session, monkeypatch):
    def fake_answer(question, history, db, client=None):
        return {"answer": None, "escalate": True}

    monkeypatch.setattr("bot.api.answer", fake_answer)
    with _client(session) as client:
        response = client.post("/chat", json={"session_id": SESSION, "message": "какая цена"})

    assert response.status_code == 200
    assert response.json() == {
        "answer": "Передаю ваш вопрос менеджеру.",
        "escalate": True,
        "sources": [],
    }
    rows = session.exec(select(ChatMessage).order_by(ChatMessage.id)).all()
    assert [(row.role, row.text, row.escalated) for row in rows] == [
        ("user", "какая цена", False),
        ("assistant", "Передаю ваш вопрос менеджеру.", True),
    ]


def test_chat_stores_history_and_passes_it_on(session, monkeypatch):
    seen: list[list[dict]] = []

    def fake_answer(question, history, db, client=None):
        seen.append(history)
        return {"answer": f"ответ: {question}", "sources": [1], "escalate": False}

    monkeypatch.setattr("bot.api.answer", fake_answer)
    with _client(session) as client:
        first = client.post("/chat", json={"session_id": SESSION, "message": "первый"})
        second = client.post("/chat", json={"session_id": SESSION, "message": "второй"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert seen[0] == []
    assert seen[1] == [
        {"role": "user", "content": "первый"},
        {"role": "assistant", "content": "ответ: первый"},
    ]
    rows = session.exec(select(ChatMessage).order_by(ChatMessage.id)).all()
    assert [(row.session_id, row.role, row.text) for row in rows] == [
        (SESSION, "user", "первый"),
        (SESSION, "assistant", "ответ: первый"),
        (SESSION, "user", "второй"),
        (SESSION, "assistant", "ответ: второй"),
    ]
