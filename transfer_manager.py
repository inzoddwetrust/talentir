from typing import Tuple, Any, Union
import logging
from aiogram import types
from aiogram.dispatcher import FSMContext

from database import User
from init import Session
from fsm_states import TransferDialog
import helpers
from variables import GlobalVariables
import config

logger = logging.getLogger(__name__)


def mask_name(name: str) -> str:
    """Маскирует имя, оставляя только первую букву и заменяя остальные на звездочки"""
    if not name:
        return ""
    return name[0] + "***"


class TransferValidator:
    """Класс для валидации вводимых данных в диалоге перевода"""

    @staticmethod
    def validate_recipient_id(user_id: str, source_balance: str, sender_id: int) -> Tuple[bool, Any]:
        """Валидирует ID получателя"""
        try:
            recipient_id = int(user_id.strip())

            # Проверяем, не пытается ли пользователь отправить средства самому себе с активного баланса
            if recipient_id == sender_id and source_balance == 'active':
                return False, "self_transfer_not_allowed"

            # Проверяем существование получателя в базе данных
            with Session() as session:
                recipient = session.query(User).filter_by(userID=recipient_id).first()
                if not recipient:
                    return False, "recipient_not_found"

            return True, recipient_id
        except ValueError:
            return False, "invalid_id_format"

    @staticmethod
    def validate_amount(amount_str: str, source_balance: str, sender_id: int) -> Tuple[bool, Any]:
        """Валидирует сумму перевода"""
        try:
            amount = float(amount_str.strip().replace(',', '.'))

            # Проверяем, что сумма положительная
            if amount <= 0:
                return False, "non_positive_amount"

            # Проверяем достаточность средств
            with Session() as session:
                sender = session.query(User).filter_by(userID=sender_id).first()
                if sender:
                    available_balance = sender.balanceActive if source_balance == 'active' else sender.balancePassive
                    if amount > available_balance:
                        return False, "insufficient_funds"

            # Рассчитываем сумму, которую получит получатель
            recipient_amount = amount
            if source_balance == 'passive':
                # Используем бонус из конфига
                bonus_percent = config.TRANSFER_BONUS
                recipient_amount = amount * (1 + bonus_percent / 100)

            return True, (amount, recipient_amount)
        except ValueError:
            return False, "invalid_amount_format"


class TransferManager:
    """Менеджер для обработки диалога перевода средств"""

    @staticmethod
    def get_state_name(state_obj: Union[str, None]) -> str:
        """Extract clean state name from State object or string"""
        if not state_obj:
            return ""
        if isinstance(state_obj, str):
            return state_obj.split(':')[1]
        return state_obj.state.split(':')[1] if hasattr(state_obj, 'state') else str(state_obj)

    @staticmethod
    async def start_transfer_dialog(message_or_callback: Union[types.Message, types.CallbackQuery],
                                    state: FSMContext) -> None:
        """Начинает диалог перевода средств, определяя источник по текущему экрану"""
        with Session() as session:
            user, success = await helpers.get_user_from_update(message_or_callback, session)
            if not success:
                return

            message_manager = GlobalVariables()._variables.get('message_manager')
            if not message_manager:
                logger.error("MessageManager not available")
                return

            # Удаляем старое сообщение, если это callback_query
            if isinstance(message_or_callback, types.CallbackQuery):
                # Определяем источник баланса по callback.data
                callback_data = message_or_callback.data

                # По умолчанию считаем, что источник - активный баланс
                source_balance = "active"

                # Если callback пришел из экрана пассивного баланса
                if callback_data.startswith("pb_") or (
                        message_or_callback.message and message_or_callback.message.text and message_or_callback.message.text.startswith(
                        "ТЕКСТ 06.2")):
                    source_balance = "passive"

                await helpers.safe_delete_message(message_or_callback)
                message = message_or_callback.message
            else:
                message = message_or_callback
                # Для обычного сообщения определить источник сложнее,
                # в этом случае можно запросить у пользователя выбор
                source_balance = "active"  # По умолчанию

            # Сбрасываем состояние FSM
            await state.finish()

            # Сохраняем данные о переводе
            await state.update_data(source_balance=source_balance, sender_id=user.userID)

            # В зависимости от источника, показываем соответствующий экран
            if source_balance == "active":
                # Для активного баланса сразу идем к вводу ID получателя
                await message_manager.send_template(
                    user=user,
                    template_key='transfer_active_enter_user_id',
                    update=message,
                    variables={
                        'balance': user.balanceActive
                    }
                )
                await TransferDialog.enter_recipient_id.set()
            else:
                # Для пассивного баланса предлагаем выбрать тип получателя
                await message_manager.send_template(
                    user=user,
                    template_key='transfer_passive_select_recipient',
                    update=message,
                    variables={
                        'balance': user.balancePassive,
                        'bonus_percent': config.TRANSFER_BONUS
                    }
                )
                await TransferDialog.select_recipient_type.set()

    @staticmethod
    async def handle_callback(callback_query: types.CallbackQuery, state: FSMContext) -> None:
        """Обрабатывает callback-запросы в диалоге перевода"""
        callback_data = callback_query.data

        with Session() as session:
            user, success = await helpers.get_user_from_update(callback_query, session)
            if not success:
                return

            message_manager = GlobalVariables()._variables.get('message_manager')
            if not message_manager:
                logger.error("MessageManager not available")
                return

            # Обрабатываем выбор типа получателя при переводе с пассивного баланса
            if callback_data == "transfer_passive_to_self":
                # Перевод самому себе
                await state.update_data(recipient_type="self", recipient_id=user.userID)

                # Здесь маскировка не нужна, так как пользователь переводит сам себе
                recipient_name = f"{user.firstname} {user.surname or ''}".strip()

                # Сразу переходим к вводу суммы
                await message_manager.send_template(
                    user=user,
                    template_key='transfer_passive_self_enter_amount',
                    update=callback_query,
                    variables={
                        'balance': user.balancePassive,
                        'recipient_name': recipient_name,
                        'recipient_id': user.userID,
                        'bonus_percent': config.TRANSFER_BONUS
                    },
                    edit=True
                )
                await TransferDialog.enter_amount.set()

            elif callback_data == "transfer_passive_to_other":
                # Перевод другому пользователю
                await state.update_data(recipient_type="other")

                await message_manager.send_template(
                    user=user,
                    template_key='transfer_passive_enter_user_id',
                    update=callback_query,
                    variables={
                        'balance': user.balancePassive
                    },
                    edit=True
                )
                await TransferDialog.enter_recipient_id.set()

            # Обработка отмены
            elif callback_data == "transfer_cancel":
                # Получаем данные о переводе
                data = await state.get_data()
                source_balance = data.get('source_balance')

                # Завершаем состояние
                await state.finish()

                # Определяем значение баланса
                balance_value = user.balanceActive if source_balance == 'active' else user.balancePassive

                # Отображаем соответствующий экран баланса
                template_key = 'active_balance' if source_balance == 'active' else 'passive_balance'

                await message_manager.send_template(
                    user=user,
                    template_key=template_key,
                    update=callback_query,
                    variables={
                        'userid': user.userID,
                        'balance': balance_value
                    },
                    edit=True
                )

    @staticmethod
    async def process_input(message: types.Message, state: FSMContext) -> None:
        """Обрабатывает ввод пользователя в диалоге перевода"""
        current_state = await state.get_state()
        state_name = TransferManager.get_state_name(current_state)

        with Session() as session:
            user, success = await helpers.get_user_from_update(message, session)
            if not success:
                return

            message_manager = GlobalVariables()._variables.get('message_manager')
            if not message_manager:
                logger.error("MessageManager not available")
                return

            # Получаем данные из состояния
            data = await state.get_data()
            source_balance = data.get('source_balance')
            sender_id = data.get('sender_id')

            # Обработка ввода ID получателя
            if state_name == 'enter_recipient_id':
                is_valid, result = TransferValidator.validate_recipient_id(
                    message.text,
                    source_balance,
                    sender_id
                )

                if not is_valid:
                    # Показываем соответствующую ошибку
                    error_template = 'transfer_error_' + result
                    await message_manager.send_template(
                        user=user,
                        template_key=error_template,
                        update=message,
                        variables={
                            'balance': user.balanceActive if source_balance == 'active' else user.balancePassive
                        }
                    )
                    return

                # Сохраняем ID получателя
                recipient_id = result
                await state.update_data(recipient_id=recipient_id)

                # Получаем информацию о получателе
                recipient = session.query(User).filter_by(userID=recipient_id).first()

                # Маскируем имя и фамилию
                masked_first_name = mask_name(recipient.firstname)
                masked_surname = mask_name(recipient.surname) if recipient.surname else ""
                recipient_name = f"{masked_first_name} {masked_surname}".strip()

                # Переходим к вводу суммы
                template_key = 'transfer_active_enter_amount' if source_balance == 'active' else 'transfer_passive_other_enter_amount'
                await message_manager.send_template(
                    user=user,
                    template_key=template_key,
                    update=message,
                    variables={
                        'balance': user.balanceActive if source_balance == 'active' else user.balancePassive,
                        'recipient_name': recipient_name,
                        'recipient_id': recipient_id,
                        'bonus_percent': config.TRANSFER_BONUS
                    }
                )
                await TransferDialog.enter_amount.set()

            # Обработка ввода суммы перевода
            elif state_name == 'enter_amount':
                is_valid, result = TransferValidator.validate_amount(
                    message.text,
                    source_balance,
                    sender_id
                )

                if not is_valid:
                    # Показываем соответствующую ошибку
                    error_template = 'transfer_error_' + result
                    await message_manager.send_template(
                        user=user,
                        template_key=error_template,
                        update=message,
                        variables={
                            'balance': user.balanceActive if source_balance == 'active' else user.balancePassive
                        }
                    )
                    return

                # Получаем сумму и сумму с бонусом
                amount, recipient_amount = result
                await state.update_data(amount=amount, recipient_amount=recipient_amount)

                # Получаем информацию о получателе
                recipient_id = data.get('recipient_id')
                recipient = session.query(User).filter_by(userID=recipient_id).first()

                # Маскируем имя получателя
                if recipient_id != user.userID:  # Если не перевод самому себе
                    # Применяем маскирование к имени и фамилии
                    masked_first_name = mask_name(recipient.firstname)
                    masked_surname = mask_name(recipient.surname) if recipient.surname else ""
                    recipient_name = f"{masked_first_name} {masked_surname}".strip()
                else:
                    # Для себя показываем полное имя
                    recipient_name = f"{recipient.firstname} {recipient.surname or ''}".strip()

                # Определяем, есть ли бонус для отображения в подтверждении
                has_bonus = source_balance == 'passive'
                bonus_text = f"+{config.TRANSFER_BONUS}% бонус" if has_bonus else ""

                # Отображаем экран подтверждения
                await message_manager.send_template(
                    user=user,
                    template_key='transfer_confirm',
                    update=message,
                    variables={
                        'amount': amount,
                        'recipient_amount': recipient_amount,
                        'recipient_name': recipient_name,
                        'recipient_id': recipient_id,
                        'bonus_text': bonus_text
                    }
                )
                await TransferDialog.confirm_transfer.set()