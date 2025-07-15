from typing import Optional, Union, Tuple
from database import User
import logging
from aiogram.types import Message, CallbackQuery
from config import REQUIRED_CHANNELS


def get_user_note(user: User, key: str) -> Optional[str]:
    """Gets value from user notes by key"""
    if not user.notes:
        return None
    notes = dict(note.split(':') for note in user.notes.split() if ':' in note)
    return notes.get(key)


def set_user_note(user: User, key: str, value: str):
    """Sets key-value pair in user notes"""
    notes = {}
    if user.notes:
        notes = dict(note.split(':') for note in user.notes.split() if ':' in note)
    notes[key] = value
    user.notes = ' '.join(f'{k}:{v}' for k, v in notes.items())


async def safe_delete_message(message_or_callback: Union[Message, CallbackQuery]) -> None:
    """
    Безопасно удаляет сообщение из чата.
    Работает как с Message, так и с CallbackQuery.

    Args:
        message_or_callback: Message или CallbackQuery объект
    """
    try:
        # Определяем тип входного объекта
        message = message_or_callback.message if isinstance(message_or_callback, CallbackQuery) else message_or_callback

        await message.bot.delete_message(
            chat_id=message.chat.id,
            message_id=message.message_id
        )
    except Exception as e:
        logging.warning(f"Failed to delete message: {e}")


async def get_user_from_update(update: Union[Message, CallbackQuery], session) -> Tuple[Optional[User], bool]:
    """
    Получает объект пользователя из базы данных.

    Args:
        update: Message или CallbackQuery объект
        session: SQLAlchemy session

    Returns:
        Tuple[Optional[User], bool]: (user, success)
        - user: объект пользователя или None
        - success: True если пользователь найден
    """
    telegram_id = update.from_user.id
    user = session.query(User).filter_by(telegramID=telegram_id).first()

    if not user:
        # Пользователь не найден, но не отправляем сообщение
        # Это нормальная ситуация для новых пользователей, использующих /start
        # Сообщение будет отправлено, только если это не обработчик /start
        is_start_command = (isinstance(update, Message) and
                            update.text and
                            update.text.startswith('/start'))

        if not is_start_command:
            if isinstance(update, CallbackQuery):
                await update.message.answer("User not found")
            else:
                await update.answer("User not found")

        return None, False

    return user, True


async def check_user_subscriptions(bot, user_id: int, user_lang: str = "en") -> tuple:
    """
    Проверяет подписку пользователя на каналы из config.REQUIRED_CHANNELS с учетом языка

    Args:
        bot: экземпляр бота
        user_id: ID пользователя в Telegram
        user_lang: язык пользователя

    Returns:
        tuple: (все_подписки_есть, список_непройденных_каналов_с_учетом_языка)
    """
    not_subscribed = []

    # Определяем каналы для проверки
    lang_channels = [c for c in REQUIRED_CHANNELS if c.get("lang") == user_lang]

    # Если нет каналов на языке пользователя, используем английские
    if not lang_channels:
        lang_channels = [c for c in REQUIRED_CHANNELS if c.get("lang") == "en"]

    # Проверяем подписки
    for channel in lang_channels:
        try:
            chat_id = channel["chat_id"]
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)

            # Проверяем статус пользователя
            if member.status in ['left', 'kicked', 'restricted']:
                not_subscribed.append(channel)

        except Exception as e:
            # Логируем ошибку, но не добавляем канал в список обязательных
            logging.error(f"Error checking subscription for {channel['chat_id']}: {e}")

    return len(not_subscribed) == 0, not_subscribed


class FakeMessage:
    def __init__(self, from_user, chat, reply_to_message=None, bot=None, args=None):
        self.from_user = from_user
        self.chat = chat
        self.reply_to_message = reply_to_message
        self.bot = bot
        self._args = args or ''  # Добавляем аргументы команды
        self.text = None  # Для совместимости с реальным Message

    async def answer(self, text, **kwargs):
        """Эмулирует message.answer(), проксируя вызов к bot.send_message"""
        return await self.bot.send_message(
            chat_id=self.chat.id,
            text=text,
            **kwargs
        )

    async def reply(self, text, **kwargs):
        """Эмулирует message.reply()"""
        return await self.bot.send_message(
            chat_id=self.chat.id,
            reply_to_message_id=None,  # Не отвечаем ни на какое сообщение
            text=text,
            **kwargs
        )

    def get_args(self) -> str:
        """
        Эмулирует message.get_args()
        Возвращает аргументы команды
        """
        return self._args
