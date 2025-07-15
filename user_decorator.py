import functools
import logging
from typing import Callable, Union
from aiogram import types

from init import Session
import helpers

# Инициализация логгера
logger = logging.getLogger(__name__)


def with_user(handler: Callable = None, *, _keep_session_open=False):
    """
    Декоратор для автоматического получения пользователя
    и передачи его в обработчик.

    Если пользователь не найден, обработчик не выполняется.

    Args:
        handler: Функция-обработчик
        _keep_session_open: Если True, сессия не будет закрыта после выполнения обработчика
    """

    def decorator(handler):
        @functools.wraps(handler)
        async def wrapper(message_or_callback: Union[types.Message, types.CallbackQuery], *args, **kwargs):
            session = Session()
            try:
                user, success = await helpers.get_user_from_update(message_or_callback, session)
                if not success:
                    session.close()
                    return

                # Передаем пользователя и сессию в обработчик
                return await handler(user, message_or_callback, session, *args, **kwargs)
            except Exception as e:
                session.rollback()
                logger.error(f"Error in handler {handler.__name__}: {e}", exc_info=True)
                raise
            finally:
                # Сессия закроется только если не указано сохранять её открытой
                if not _keep_session_open:
                    session.close()

        return wrapper

    # Позволяет использовать декоратор как с параметрами, так и без
    if handler is None:
        return decorator
    return decorator(handler)