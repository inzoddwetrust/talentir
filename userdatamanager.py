from datetime import datetime
import re
import logging
from typing import Tuple, Any, Dict, Union
from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State

import secrets
import string
from email_sender import email_manager

from database import User
from init import Session
from fsm_states import UserDataDialog
from templates import MessageTemplates
import helpers
from variables import GlobalVariables

FIELD_CONFIG = {
    'waiting_for_firstname': {
        'field': 'firstname',
        'validator': 'validate_name',
        'template_request': 'user_data_firstname',
        'template_error': 'user_data_firstname_error',
        'next_state': UserDataDialog.waiting_for_surname
    },
    'waiting_for_surname': {
        'field': 'surname',
        'validator': 'validate_name',
        'template_request': 'user_data_surname',
        'template_error': 'user_data_surname_error',
        'next_state': UserDataDialog.waiting_for_birthday
    },
    'waiting_for_birthday': {
        'field': 'birthday',
        'validator': 'validate_date',
        'template_request': 'user_data_birthday',
        'template_error': 'user_data_birthday_error',
        'next_state': UserDataDialog.waiting_for_passport
    },
    'waiting_for_passport': {  # New state
        'field': 'passport',
        'validator': 'validate_passport',
        'template_request': 'user_data_passport',
        'template_error': 'user_data_passport_error',
        'next_state': UserDataDialog.waiting_for_country
    },
    'waiting_for_country': {
        'field': 'country',
        'validator': 'validate_text',
        'template_request': 'user_data_country',
        'template_error': 'user_data_country_error',
        'next_state': UserDataDialog.waiting_for_city
    },
    'waiting_for_city': {
        'field': 'city',
        'validator': 'validate_text',
        'template_request': 'user_data_city',
        'template_error': 'user_data_city_error',
        'next_state': UserDataDialog.waiting_for_address
    },
    'waiting_for_address': {
        'field': 'address',
        'validator': 'validate_text',
        'template_request': 'user_data_address',
        'template_error': 'user_data_address_error',
        'next_state': UserDataDialog.waiting_for_phone
    },
    'waiting_for_phone': {
        'field': 'phoneNumber',
        'validator': 'validate_phone',
        'template_request': 'user_data_phone',
        'template_error': 'user_data_phone_error',
        'next_state': UserDataDialog.waiting_for_email
    },
    'waiting_for_email': {
        'field': 'email',
        'validator': 'validate_email',
        'template_request': 'user_data_email',
        'template_error': 'user_data_email_error',
        'next_state': UserDataDialog.waiting_for_confirmation
    }
}

def generate_verification_token():
    """Генерирует 16-символьный токен для верификации email"""
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(16))


class FieldValidator:
    @staticmethod
    def validate_name(value: str) -> Tuple[bool, Any]:
        """Validates first and last names"""
        value = value.strip()
        if not value.isalpha() or not value[0].isupper():
            return False, None
        return True, value

    @staticmethod
    def validate_date(value: str) -> Tuple[bool, Any]:
        """Validates date in dd.mm.yyyy format"""
        try:
            parsed_date = datetime.strptime(value.strip(), "%d.%m.%Y")
            return True, parsed_date
        except ValueError:
            return False, None

    @staticmethod
    def validate_passport(value: str) -> Tuple[bool, Any]:
        """Validates passport number"""
        value = value.strip()
        if len(value) < 6 or len(value) > 20:  # Adjust these limits as needed
            return False, None
        # Remove spaces and special characters, keep only alphanumeric
        cleaned = ''.join(c for c in value if c.isalnum() or c in '/-')
        if not cleaned:
            return False, None
        return True, cleaned

    @staticmethod
    def validate_phone(value: str) -> Tuple[bool, Any]:
        """Validates phone number"""
        value = value.strip()

        if value.startswith("+"):
            value = value[1:]

        if not value.isdigit():
            return False, None

        return True, value

    @staticmethod
    def validate_email(value: str) -> Tuple[bool, Any]:
        """Validates email address"""
        value = value.strip()
        if not re.match(r"[^@]+@[^@]+\.[^@]+", value):
            return False, None
        return True, value

    @staticmethod
    def validate_text(value: str) -> Tuple[bool, Any]:
        """Validates general text input"""
        value = value.strip()
        if not value:
            return False, None
        return True, value


class UserDataManager:
    @staticmethod
    def get_state_name(state_obj: Union[State, str]) -> str:
        """Extract clean state name from State object or string"""
        if isinstance(state_obj, str):
            return state_obj.split(':')[1]
        return state_obj.state.split(':')[1] if hasattr(state_obj, 'state') else str(state_obj)

    @staticmethod
    def find_previous_state(current_state_name: str) -> State:
        """Find state that points to current state in FIELD_CONFIG"""
        # Для первого состояния возвращаем его же
        if current_state_name == 'waiting_for_firstname':
            return UserDataDialog.waiting_for_firstname

        # Ищем состояние, которое указывает на текущее
        for state_name, config in FIELD_CONFIG.items():
            next_state_name = UserDataManager.get_state_name(config['next_state'])
            if next_state_name == current_state_name:
                return getattr(UserDataDialog, state_name)

        # В случае ошибки возвращаем начальное состояние
        logging.error(f"Previous state not found for {current_state_name}")
        return UserDataDialog.waiting_for_firstname

    @staticmethod
    async def handle_navigation(
            callback_query: types.CallbackQuery,
            state: FSMContext,
            direction: str
    ) -> None:
        """Handles navigation between states (back/restart/cancel)"""
        # Получаем message_manager из GlobalVariables
        message_manager = GlobalVariables()._variables.get('message_manager')
        if not message_manager:
            logging.error("MessageManager not available in UserDataManager.handle_navigation")
            return

        if direction == 'cancel':
            await state.finish()
            with Session() as session:
                user, success = await helpers.get_user_from_update(callback_query, session)
                if not success:
                    return

            # Используем message_manager вместо прямого создания сообщения
            await message_manager.send_template(
                user=user,
                template_key='user_data_cancelled',
                update=callback_query,
                variables={},
                edit=True
            )
            return

        if direction == 'restart':
            await state.finish()
            await UserDataManager.start_user_data_dialog(callback_query, state)
            return

        if direction == 'back':
            current_state = await state.get_state()
            if not current_state:
                return

            # Получаем имя текущего состояния без префикса
            current_state_name = UserDataManager.get_state_name(current_state)

            # Находим предыдущее состояние
            previous_state = UserDataManager.find_previous_state(current_state_name)
            previous_state_name = UserDataManager.get_state_name(previous_state)

            with Session() as session:
                user, success = await helpers.get_user_from_update(callback_query, session)
                if not success:
                    return

            field_config = FIELD_CONFIG.get(previous_state_name)
            if not field_config:
                logging.error(f"No field config found for state {previous_state_name}")
                return

            # Используем message_manager вместо генерации экрана и ручной отправки
            await message_manager.send_template(
                user=user,
                template_key=field_config['template_request'],
                update=callback_query,
                variables={},
                edit=True
            )
            await previous_state.set()

    @staticmethod
    async def validate_and_process_input(
            message: types.Message,
            state: FSMContext,
            field_config: Dict
    ) -> Tuple[bool, Any]:
        """Validates user input and processes it according to field configuration"""
        validator = getattr(FieldValidator, field_config['validator'])
        is_valid, value = validator(message.text)

        if not is_valid:
            with Session() as session:
                user, success = await helpers.get_user_from_update(message, session)
                if not success:
                    return False, None

            text, media_id, keyboard, parse_mode, disable_preview = await MessageTemplates.generate_screen(
                user,
                field_config['template_error'],
                {}
            )

            await message.answer(
                text=text,
                parse_mode=parse_mode,
                reply_markup=keyboard,
                disable_web_page_preview=disable_preview
            )
            return False, None

        return True, value

    @staticmethod
    async def show_next_input_request(
            message: types.Message,
            next_state: State,
            field_config: Dict
    ) -> None:
        """Shows request for next input field"""
        with Session() as session:
            user, success = await helpers.get_user_from_update(message, session)
            if not success:
                return

        text, media_id, keyboard, parse_mode, disable_preview = await MessageTemplates.generate_screen(
            user,
            field_config['template_request'],
            {}
        )

        await message.answer(
            text=text,
            parse_mode=parse_mode,
            reply_markup=keyboard,
            disable_web_page_preview=disable_preview
        )
        await next_state.set()

    @staticmethod
    async def process_input(message: types.Message, state: FSMContext) -> None:
        """Process user input and move to next state if valid"""
        current_state = await state.get_state()
        state_name = current_state.split(':')[1]  # Получаем часть после ':'

        field_config = FIELD_CONFIG.get(state_name)
        if not field_config:
            await state.finish()
            return

        # Validate and process input
        is_valid, value = await UserDataManager.validate_and_process_input(
            message, state, field_config
        )
        if not is_valid:
            return

        # Save valid value
        await state.update_data({field_config['field']: value})

        # Get next state from config
        next_state = field_config['next_state']

        if next_state == UserDataDialog.waiting_for_confirmation:
            await UserDataManager.show_confirmation(message, state)
        else:
            next_state_name = UserDataManager.get_state_name(next_state)  # <-- Исправленная строка
            next_config = FIELD_CONFIG.get(next_state_name)
            await UserDataManager.show_next_input_request(message, next_state, next_config)

    @staticmethod
    async def show_confirmation(message: types.Message, state: FSMContext) -> None:
        """Shows confirmation screen with collected data"""
        user_data = await state.get_data()

        with Session() as session:
            user, success = await helpers.get_user_from_update(message, session)
            if not success:
                return

        # Format data for template
        context = {
            'firstname': user_data['firstname'],
            'surname': user_data['surname'],
            'birthday': user_data['birthday'].strftime('%d.%m.%Y'),
            'passport': user_data['passport'],
            'country': user_data['country'],
            'city': user_data['city'],
            'address': user_data['address'],
            'phone': user_data['phoneNumber'],
            'email': user_data['email']
        }

        text, media_id, keyboard, parse_mode, disable_preview = await MessageTemplates.generate_screen(
            user,
            'user_data_confirmation',
            context
        )

        await message.answer(
            text=text,
            parse_mode=parse_mode,
            reply_markup=keyboard,
            disable_web_page_preview=disable_preview
        )
        await UserDataDialog.waiting_for_confirmation.set()

    @staticmethod
    async def start_user_data_dialog(message_or_callback: Union[types.Message, types.CallbackQuery],
                                     state: FSMContext) -> None:
        """Starts or restarts the user data collection dialog"""
        with Session() as session:
            user, success = await helpers.get_user_from_update(message_or_callback, session)
            if not success:
                return

        if isinstance(message_or_callback, types.CallbackQuery):
            await helpers.safe_delete_message(message_or_callback)
            message = message_or_callback.message
        else:
            message = message_or_callback

        text, media_id, keyboard, parse_mode, disable_preview = await MessageTemplates.generate_screen(
            user,
            'user_data_firstname',
            {}
        )

        await message.answer(
            text=text,
            parse_mode=parse_mode,
            reply_markup=keyboard,
            disable_web_page_preview=disable_preview
        )
        await UserDataDialog.waiting_for_firstname.set()

    @staticmethod
    async def save_user_data(user: User, user_data: Dict) -> bool:
        """Saves user data to database and syncs with Google Sheets"""
        try:
            with Session() as session:
                # Get fresh user object in this session
                db_user = session.query(User).filter_by(userID=user.userID).first()
                if not db_user:
                    logging.error(f"User {user.userID} not found in database")
                    return False

                # Update user data
                for field, value in user_data.items():
                    setattr(db_user, field, value)
                db_user.isFilled = True

                # Commit changes
                session.commit()

                # Refresh object after commit
                session.refresh(db_user)

                return True
        except Exception as e:
            logging.error(f"Error saving user data: {e}")
            return False
