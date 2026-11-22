import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlmodel import Session

from app.db import engine, init_db
from app.models import ChatMessage
from app.settings import get_settings
from bot.api import handle_message

logger = logging.getLogger(__name__)

GREETING = (
    "Здравствуйте! Я консультант по оптике и электронике. "
    "Задайте вопрос, и я отвечу по материалам компании."
)

router = Router()


def client_contact(message: Message) -> str:
    user = message.from_user
    if user is None:
        return f"chat id {message.chat.id}"
    if user.username:
        return f"{user.full_name} (@{user.username}, id {user.id})"
    return f"{user.full_name} (id {user.id})"


def manager_notice(question: str, contact: str) -> str:
    return f"Клиент просит менеджера.\nКонтакт: {contact}\nВопрос: {question}"


@router.message(CommandStart())
async def start(message: Message) -> None:
    with Session(engine) as session:
        session.add(
            ChatMessage(
                session_id=str(message.chat.id),
                role="assistant",
                text=GREETING,
                escalated=False,
            )
        )
        session.commit()
    await message.answer(GREETING)


@router.message(F.text)
async def reply(message: Message, bot: Bot) -> None:
    text = message.text or ""
    if text.startswith("/"):
        return
    with Session(engine) as session:
        result = handle_message(session, str(message.chat.id), text)
    await message.answer(result.answer)
    if result.escalate:
        await notify_manager(bot, text, client_contact(message))


async def notify_manager(bot: Bot, question: str, contact: str) -> None:
    chat_id = get_settings().telegram_manager_chat_id.strip()
    if not chat_id:
        logger.warning("TELEGRAM_MANAGER_CHAT_ID is empty, escalation was not delivered")
        return
    try:
        await bot.send_message(int(chat_id), manager_notice(question, contact))
    except Exception:
        logger.exception("failed to notify manager chat %s", chat_id)


async def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    init_db()
    bot = Bot(token=get_settings().telegram_bot_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await dispatcher.start_polling(bot)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
