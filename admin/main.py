# Streamlit puts this file's directory on sys.path. The name app.py would hide the app package.
from datetime import date, timedelta

import pandas as pd
import streamlit as st
from sqlmodel import Session

from admin.actions import (
    bot_metrics,
    funnel,
    get_pipeline_mode,
    publish_from_review,
    read_last_run_errors,
    recent_audit,
    reject_from_review,
    review_queue,
    set_pipeline_mode,
    usage_report,
)
from app.db import engine, init_db
from app.models import ContentItem
from app.settings import get_settings


def main() -> None:
    st.set_page_config(page_title="Optics Copilot", layout="wide")
    if not _authenticated():
        return
    page = st.sidebar.radio("Раздел", ["Очередь ревью", "Дашборд"])
    if page == "Очередь ревью":
        _review_page()
    else:
        _dashboard_page()


def _authenticated() -> bool:
    if st.session_state.get("admin_authenticated"):
        return True
    st.title("Вход")
    password = st.text_input("Пароль", type="password")
    if st.button("Войти"):
        expected = get_settings().admin_password
        if expected and password == expected:
            st.session_state.admin_authenticated = True
            st.rerun()
        st.error("Неверный пароль")
    return False


def _review_page() -> None:
    st.title("Очередь ревью")
    _mode_switch()
    with _session() as session:
        items = review_queue(session)
        rows = [_queue_row(item) for item in items]
    if not rows:
        st.info("Очередь пуста")
        return
    table = pd.DataFrame(rows)
    event = st.dataframe(
        table,
        hide_index=True,
        column_order=["Заголовок", "Тип", "Причина", "Дата"],
        on_select="rerun",
        selection_mode="single-row",
    )
    selected = event.selection.rows
    if not selected:
        st.caption("Выберите запись в таблице")
        return
    item_id = int(rows[selected[0]]["id"])
    with _session() as session:
        item = session.get(ContentItem, item_id)
        detail = None if item is None else _detail(item)
    if detail is None:
        st.warning("Запись не найдена")
        return
    original, translation = st.columns(2)
    with original:
        st.subheader("Оригинал")
        st.text_area("Заголовок оригинала", detail["title_original"], disabled=True)
        st.text_area("Текст оригинала", detail["body_original"], height=320, disabled=True)
    with translation:
        st.subheader("Перевод")
        title = st.text_area("Заголовок", detail["title_ru"], key=f"title-{detail['id']}")
        body = st.text_area("Текст", detail["body_ru"], height=320, key=f"body-{detail['id']}")
    publish, reject = st.columns(2)
    with publish:
        if st.button("Опубликовать"):
            _publish(detail["id"], title, body)
    with reject:
        if st.button("Отклонить"):
            _reject(detail["id"])


def _mode_switch() -> None:
    with _session() as session:
        current = get_pipeline_mode(session).value
    choice = st.radio(
        "Режим пайплайна",
        ["manual", "auto"],
        index=0 if current == "manual" else 1,
        horizontal=True,
    )
    if choice == current:
        return
    if choice == "auto":
        st.warning("Автоматический режим публикует материалы без ручной проверки.")
        if st.button("Включить автоматический режим"):
            with _session() as session:
                set_pipeline_mode(session, "auto")
            st.rerun()
        return
    with _session() as session:
        set_pipeline_mode(session, "manual")
    st.rerun()


def _publish(item_id: int, title: str, body: str) -> None:
    try:
        with _session() as session:
            publish_from_review(session, item_id, title, body)
    except Exception as exc:
        st.error(f"Не удалось опубликовать: {exc}")
        return
    st.success("Опубликовано")
    st.rerun()


def _reject(item_id: int) -> None:
    try:
        with _session() as session:
            reject_from_review(session, item_id)
    except Exception as exc:
        st.error(f"Не удалось отклонить: {exc}")
        return
    st.success("Отклонено")
    st.rerun()


def _dashboard_page() -> None:
    st.title("Дашборд")
    today = date.today()
    period = st.date_input(
        "Период",
        value=(today - timedelta(days=30), today),
    )
    if not isinstance(period, tuple) or len(period) != 2:
        st.info("Выберите начало и конец периода")
        return
    start, end = period
    with _session() as session:
        stages = funnel(session, start, end)
        usage = usage_report(session, start, end)
        bots = bot_metrics(session, start, end)
        audit = [_audit_row(row) for row in recent_audit(session)]
    st.subheader("Воронка")
    st.bar_chart(pd.DataFrame(stages, columns=["Этап", "Количество"]).set_index("Этап"))
    st.subheader("Расход")
    st.metric("Сумма за период, USD", f"{usage['total']:.4f}")
    if usage["by_day"]:
        daily = pd.DataFrame(usage["by_day"], columns=["День", "USD"]).set_index("День")
        st.line_chart(daily)
    else:
        st.info("За период нет расхода токенов")
    if usage["by_purpose"]:
        st.dataframe(
            pd.DataFrame(
                [{"Назначение": key, "USD": value} for key, value in usage["by_purpose"].items()]
            ),
            hide_index=True,
        )
    st.subheader("Бот")
    dialogs, messages, share = st.columns(3)
    dialogs.metric("Диалогов", bots["dialogs"])
    messages.metric("Сообщений", bots["messages"])
    share.metric("Доля эскалаций", f"{bots['escalated_share']:.0%}")
    st.subheader("Журнал")
    st.dataframe(pd.DataFrame(audit), hide_index=True)
    st.subheader("Ошибки последнего прогона")
    errors = read_last_run_errors()
    if errors:
        st.code(errors)
    else:
        st.info("Ошибок последнего прогона нет")


def _queue_row(item: ContentItem) -> dict:
    moment = item.updated_at.isoformat(timespec="minutes")
    return {
        "id": item.id,
        "Заголовок": item.title_ru or item.title_original,
        "Тип": item.source_type.value,
        "Причина": item.review_reason,
        "Дата": moment,
    }


def _detail(item: ContentItem) -> dict:
    return {
        "id": item.id,
        "title_original": item.title_original,
        "body_original": item.body_original,
        "title_ru": item.title_ru,
        "body_ru": item.body_ru,
    }


def _audit_row(row) -> dict:
    return {
        "Дата": row.created_at.isoformat(timespec="minutes"),
        "Запись": row.item_id,
        "Действие": row.action,
        "Было": row.old_status.value if row.old_status else "",
        "Стало": row.new_status.value,
        "Режим": row.mode.value,
        "Кто": row.actor,
    }


def _session():
    init_db()
    return Session(engine)


if __name__ == "__main__":
    main()
