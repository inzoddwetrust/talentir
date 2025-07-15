from typing import Optional, Dict, Tuple, List, Union, Any
from aiogram import types

from google_services import get_google_services
from config import GOOGLE_SHEET_ID


class SafeDict(dict):
    def __missing__(self, key):

        try:
            # Разделяем ключ и спецификатор формата (если есть)
            base_key = key.split(':')[0]
            # Если ключ отсутствует, возвращаем нулевое значение с учетом формата
            if ':' in key:
                format_spec = key.split(':', 1)[1]
                if 'f' in format_spec:  # Числовой формат
                    return format(0, format_spec)
                elif 'd' in format_spec:  # Целочисленный формат
                    return format(0, format_spec)
            # Для ключей без форматирования просто возвращаем в фигурных скобках
            return '{' + base_key + '}'
        except Exception:
            # В случае любых проблем с форматированием возвращаем безопасное значение
            return '{' + key + '}'


class MessageTemplates:
    _cache: Dict[Tuple[str, str], Dict] = {}

    @staticmethod
    async def load_templates():
        sheets_client, _ = get_google_services()
        sheet = sheets_client.open_by_key(GOOGLE_SHEET_ID).worksheet("Templates")
        rows = sheet.get_all_records()

        MessageTemplates._cache = {
            (row['stateKey'], row['lang']): {
                'text': row['text'],
                'parseMode': row['parseMode'],
                'disablePreview': row['disablePreview'],
                'mediaType': row['mediaType'],
                'mediaID': row['mediaID'],
                'buttons': row['buttons']
            } for row in rows
        }

    @staticmethod
    async def get_template(state_key: str, lang: str = 'en') -> Optional[Dict]:
        if not MessageTemplates._cache:
            await MessageTemplates.load_templates()

        template = MessageTemplates._cache.get((state_key, lang))
        if not template:
            template = MessageTemplates._cache.get((state_key, 'en'))

        return template

    @staticmethod
    async def get_raw_template(state_key: str, variables: dict, lang: str = 'en') -> tuple[str, Optional[str]]:
        """
        Gets raw template without media formatting.
        Used primarily for notifications.

        Args:
            state_key: ID шаблона
            variables: Словарь с переменными для подстановки
            lang: Код языка (по умолчанию 'en')

        Returns:
            tuple[str, Optional[str]]: (отформатированный текст, отформатированные кнопки в JSON)
        """
        if not MessageTemplates._cache:
            await MessageTemplates.load_templates()

        template = MessageTemplates._cache.get((state_key, lang))
        if not template:
            template = MessageTemplates._cache.get((state_key, 'en'))
            if not template:
                raise ValueError(f"Template not found: {state_key}")

        text = template['text'].replace('\\n', '\n')
        buttons = template['buttons']

        if 'rgroup' in variables:
            text = MessageTemplates.process_repeating_group(text, variables['rgroup'])
            if buttons:
                buttons = MessageTemplates.process_repeating_group(buttons, variables['rgroup'])

        formatted_text = text.format_map(SafeDict(variables))
        if buttons:
            formatted_buttons = buttons.format_map(SafeDict(variables))
        else:
            formatted_buttons = None

        return formatted_text, formatted_buttons

    @staticmethod
    def sequence_format(template: str, variables: dict, sequence_index: int = 0) -> str:
        """
        Formats string with variables, supporting both scalar and sequence values.
        For sequence values, uses value at sequence_index or last value if index out of range.
        """
        formatted_vars = {}

        for key, value in variables.items():
            if isinstance(value, (list, tuple)):
                # If value is a sequence, take value at index or last value
                try:
                    formatted_vars[key] = value[min(sequence_index, len(value) - 1)]
                except (IndexError, ValueError):
                    continue
            else:
                formatted_vars[key] = value

        return template.format_map(SafeDict(formatted_vars))

    @staticmethod
    def create_keyboard(buttons_str: str, variables: dict = None) -> Optional[types.InlineKeyboardMarkup]:
        """
        Creates keyboard object from configuration string with variable support.
        Supports both scalar and sequence variables, applying sequence values in order.
        """
        if not buttons_str or not buttons_str.strip():
            return None

        try:
            keyboard = types.InlineKeyboardMarkup()

            rows = buttons_str.split('\n')

            sequence_index = 0

            for row in rows:
                if not row.strip():
                    continue

                button_row = []
                buttons = row.split(';')

                for button in buttons:
                    button = button.strip()
                    if not button or ':' not in button:
                        continue

                    # Для webapp и url, нужно быть осторожными с разделителем ":"
                    # Поэтому сначала проверяем эти префиксы
                    if '|webapp|' in button:
                        # Разделяем текст и URL для webapp
                        # Формат: |webapp|http://example.com:Текст кнопки
                        webapp_parts = button.split(':', 1)

                        # Нам нужно вручную разобрать webapp-часть
                        webapp_url_part = webapp_parts[0].strip()
                        button_text = webapp_parts[1].strip() if len(webapp_parts) > 1 else "Open WebApp"

                        # Проверяем, что URL начинается с |webapp|
                        if webapp_url_part.startswith('|webapp|'):
                            url = webapp_url_part[8:]  # Удаляем префикс |webapp|

                            # Добавляем протокол http:// если не указан
                            if not url.startswith(('http://', 'https://')):
                                url = 'https://' + url

                            # Применяем переменные к тексту и URL если они предоставлены
                            if variables:
                                try:
                                    button_text = MessageTemplates.sequence_format(
                                        button_text, variables, sequence_index
                                    )
                                    if '{}' in url or '{' in url:
                                        url = MessageTemplates.sequence_format(
                                            url, variables, sequence_index
                                        )
                                    sequence_index += 1
                                except Exception as e:
                                    print(f"Error formatting webapp button: {e}")
                                    continue

                            try:
                                # Создаем webapp кнопку
                                from aiogram.types import WebAppInfo
                                button_row.append(
                                    types.InlineKeyboardButton(
                                        text=button_text,
                                        web_app=WebAppInfo(url=url)
                                    )
                                )
                                # Переходим к следующей кнопке
                                continue  # Важно! Пропускаем остальную обработку для этой кнопки
                            except Exception as e:
                                print(f"Error creating webapp button: {e}")

                    elif '|url|' in button:
                        # Разделяем текст и URL для обычной URL-кнопки
                        # Формат: |url|example.com:Текст кнопки
                        url_parts = button.split(':', 1)
                        url_part = url_parts[0].strip()
                        button_text = url_parts[1].strip() if len(url_parts) > 1 else "Open URL"

                        # Проверяем, что URL начинается с |url|
                        if url_part.startswith('|url|'):
                            url = url_part[5:]  # Удаляем префикс |url|

                            # Добавляем протокол http:// если не указан
                            if not url.startswith(('http://', 'https://')):
                                url = 'http://' + url

                            # Применяем переменные к тексту и URL если они предоставлены
                            if variables:
                                try:
                                    button_text = MessageTemplates.sequence_format(
                                        button_text, variables, sequence_index
                                    )
                                    if '{}' in url or '{' in url:
                                        url = MessageTemplates.sequence_format(
                                            url, variables, sequence_index
                                        )
                                    sequence_index += 1
                                except Exception as e:
                                    print(f"Error formatting url button: {e}")
                                    continue

                            try:
                                # Создаем URL-кнопку
                                button_row.append(
                                    types.InlineKeyboardButton(
                                        text=button_text,
                                        url=url
                                    )
                                )
                                # Переходим к следующей кнопке
                                continue  # Важно! Пропускаем остальную обработку для этой кнопки
                            except Exception as e:
                                print(f"Error creating url button: {e}")

                    # Стандартная обработка для callback-кнопок
                    callback, text = button.split(':', 1)
                    callback, text = callback.strip(), text.strip()

                    # Format both callback and text with variables if provided
                    if variables:
                        try:
                            text = MessageTemplates.sequence_format(
                                text, variables, sequence_index
                            )
                            callback = MessageTemplates.sequence_format(
                                callback, variables, sequence_index
                            )
                            sequence_index += 1
                        except Exception as e:
                            print(f"Error formatting callback button: {e}")
                            continue

                    try:
                        # Создаем обычную callback-кнопку
                        button_row.append(
                            types.InlineKeyboardButton(
                                text=text,
                                callback_data=callback
                            )
                        )
                    except Exception as e:
                        print(f"Error creating callback button: {e}")

                if button_row:
                    keyboard.row(*button_row)

            return keyboard if keyboard.inline_keyboard else None

        except Exception as e:
            print(f"Error creating keyboard: {e}")
            return None

    @staticmethod
    def merge_buttons(buttons_list: List[str]) -> str:
        """Merges multiple button configurations into a single string."""
        valid_configs = [b.strip() for b in buttons_list if b and b.strip()]
        if not valid_configs:
            return ''

        # Collect all rows from each configuration
        all_rows = []

        for config in valid_configs:
            # Split by newlines
            rows = config.split('\n')

            # Add each non-empty row
            for row in rows:
                if row.strip():
                    all_rows.append(row.strip())

        # Join all rows with newlines
        return '\n'.join(all_rows)

    @staticmethod
    def process_repeating_group(template_text: str, rgroup_data: Dict[str, List[Any]]) -> str:
        """Processes repeating group in template text."""
        start = template_text.find('|rgroup:')
        if start == -1:
            return template_text

        end = template_text.find('|', start + 8)
        if end == -1:
            return template_text

        item_template = template_text[start + 8:end]
        full_template = template_text[start:end + 1]

        if not rgroup_data or not all(rgroup_data.values()):
            return template_text.replace(full_template, '')

        lengths = {len(arr) for arr in rgroup_data.values()}
        if len(lengths) != 1:
            print(f"Inconsistent lengths in rgroup data: {lengths}")
            return template_text.replace(full_template, '')

        result = []
        for i in range(next(iter(lengths))):
            item_data = {key: values[i] for key, values in rgroup_data.items()}
            result.append(item_template.format_map(SafeDict(item_data)))

        return template_text.replace(full_template, '\n'.join(result))

    @classmethod
    async def generate_screen(
            cls,
            user,
            state_keys: Union[str, List[str]],
            variables: Optional[dict] = None  # Делаем опциональным здесь
    ) -> Tuple[str, Optional[str], Optional[types.InlineKeyboardMarkup], str, bool]:
        """
        Generates screen content from templates.

        Args:
            user: User object for localization
            state_keys: Template key or list of keys
            variables: Optional dictionary with template variables
        """
        if isinstance(state_keys, str):
            state_keys = [state_keys]

        templates = []
        for key in state_keys:
            template = cls._cache.get((key, user.lang)) or cls._cache.get((key, 'en'))
            if not template:
                print(f"Template not found for state {key}")
                continue
            templates.append(template)

        if not templates:
            # Пробуем получить специальный шаблон fallback
            fallback = cls._cache.get(('fallback', user.lang)) or cls._cache.get(('fallback', 'en'))
            if fallback:
                templates = [fallback]
            else:
                print("Fallback template not found")
                return "Template not found", None, None, None, True

        try:
            texts = []
            buttons_list = []
            format_vars = (variables or {}).copy()  # Безопасная инициализация и копирование

            for template in templates:
                text = template['text'].replace('\\n', '\n')

                if 'rgroup' in format_vars:
                    text = cls.process_repeating_group(text, format_vars['rgroup'])

                text = text.format_map(SafeDict(format_vars))
                texts.append(text)

                if template['buttons']:
                    buttons_list.append(template['buttons'])

            final_text = '\n\n'.join(text for text in texts if text)
            merged_buttons = cls.merge_buttons(buttons_list)
            keyboard = cls.create_keyboard(merged_buttons, variables=format_vars)

            first_template = templates[0]
            media_id = first_template['mediaID'] if first_template['mediaType'] != 'None' else None
            parse_mode = first_template['parseMode']
            disable_preview = first_template['disablePreview']

            return final_text, media_id, keyboard, parse_mode, disable_preview

        except Exception as e:
            print(f"Error generating screen: {str(e)}")
            return f"Error generating screen: {str(e)}", None, None, None, True
