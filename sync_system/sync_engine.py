"""
Универсальный движок синхронизации БД <-> Google Sheets
Заменяет старые imports.py и exports.py.old
ПРИНЦИП: БД - это истина, таблица может содержать ошибки
"""

import logging
from typing import Dict, List, Any, Tuple, Optional
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from google_services import get_google_services
from sync_system.sync_config import (
    SYNC_CONFIG,
    validate_upliner,
    validate_foreign_key
)
import config

logger = logging.getLogger(__name__)


class UniversalSyncEngine:
    """Универсальный движок для синхронизации любой таблицы"""

    def __init__(self, table_name: str):
        if table_name not in SYNC_CONFIG:
            raise ValueError(f"Unknown table: {table_name}")

        self.table_name = table_name
        self.config = SYNC_CONFIG[table_name]
        self.model = self.config['model']
        self.primary_key = self.config['primary_key']
        self.sheet_name = self.config['sheet_name']

    def export_to_json(self, session: Session) -> Dict[str, Any]:
        """
        Экспортирует данные из БД в JSON для отправки в Google Sheets
        Используется webhook'ом
        """
        try:
            # Получаем все записи
            records = session.query(self.model).all()

            # Преобразуем в JSON-совместимый формат
            data = []
            for record in records:
                row = {}
                for column in self.model.__table__.columns:
                    value = getattr(record, column.name)

                    # Преобразуем специальные типы
                    if isinstance(value, datetime):
                        value = value.strftime("%Y-%m-%d %H:%M:%S")
                    elif value is None:
                        value = ""
                    elif isinstance(value, bool):
                        value = int(value)

                    row[column.name] = value
                data.append(row)

            return {
                'success': True,
                'table': self.table_name,
                'rows': data,
                'count': len(data),
                'timestamp': datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f"Export error for {self.table_name}: {e}")
            return {
                'success': False,
                'error': str(e),
                'table': self.table_name
            }

    def import_from_sheets(self, session: Session, dry_run: bool = False) -> Dict[str, Any]:
        """
        Импортирует данные из Google Sheets в БД

        Args:
            session: SQLAlchemy сессия
            dry_run: Если True, только показывает что изменится, не применяет
        """
        results = {
            'table': self.table_name,
            'total': 0,
            'updated': 0,
            'added': 0,
            'skipped': 0,
            'errors': [],
            'changes': []
        }

        try:
            # Подключаемся к Google Sheets
            sheets_client, _ = get_google_services()
            spreadsheet = sheets_client.open_by_key(config.GOOGLE_SHEET_ID)
            sheet = spreadsheet.worksheet(self.sheet_name)

            # Получаем сырые данные
            raw_records = sheet.get_all_records()

            # ОЧИСТКА ДАННЫХ ИЗ GOOGLE SHEETS
            sheet_records = []
            for idx, raw_row in enumerate(raw_records):
                clean_row = {}
                for key, value in raw_row.items():
                    # Убираем пробелы и невидимые символы из ключей
                    clean_key = key.strip().replace('\u200b', '').replace('\xa0', ' ')

                    # Обработка значений
                    if value == '' or value is None:
                        clean_row[clean_key] = None
                    elif isinstance(value, str):
                        # Убираем невидимые символы из значений
                        clean_value = value.strip().replace('\u200b', '').replace('\xa0', ' ')
                        clean_row[clean_key] = clean_value if clean_value else None
                    else:
                        clean_row[clean_key] = value

                sheet_records.append(clean_row)

            results['total'] = len(sheet_records)

            # Для Users собираем все существующие telegramID
            existing_telegram_ids = set()
            if self.table_name == 'Users':
                existing_users = session.query(self.model).all()
                existing_telegram_ids = {user.telegramID for user in existing_users}

            # Обрабатываем каждую строку
            consecutive_errors = 0
            last_error = None

            for row_idx, row in enumerate(sheet_records, start=2):
                try:
                    # Проверка обязательных полей для Users
                    if self.table_name == 'Users':
                        telegram_id = row.get('telegramID')
                        if not telegram_id:
                            logger.warning(f"Row {row_idx}: Skipping user {row.get('userID')} - no telegramID")
                            results['skipped'] += 1
                            results['errors'].append({
                                'row': row_idx,
                                'error': 'Missing telegramID',
                                'id': row.get('userID')
                            })
                            continue

                    # Обрабатываем строку
                    result = self._process_row(session, row, row_idx, dry_run)

                    if result['action'] == 'update':
                        results['updated'] += 1
                        if result.get('changes'):
                            results['changes'].append({
                                'row': row_idx,
                                'id': row.get(self.primary_key),
                                'action': 'update',
                                'fields': result['changes']
                            })
                    elif result['action'] == 'add':
                        results['added'] += 1
                        results['changes'].append({
                            'row': row_idx,
                            'id': row.get(self.primary_key),
                            'action': 'add'
                        })
                    elif result['action'] == 'skip':
                        results['skipped'] += 1
                    elif result['action'] == 'error':
                        # Добавляем ошибку в отчет
                        if 'error' in result:
                            results['errors'].append({
                                'row': row_idx,
                                'error': result['error'],
                                'id': row.get('telegramID' if self.table_name == 'Users' else self.primary_key)
                            })

                    consecutive_errors = 0  # Сбрасываем счетчик ошибок

                except Exception as e:
                    error_msg = str(e)[:500]

                    # Откатываем транзакцию
                    if not dry_run:
                        try:
                            session.rollback()
                        except:
                            session.close()
                            from init import Session
                            session = Session()

                    logger.error(f"Error processing row {row_idx}: {error_msg}")
                    results['errors'].append({
                        'row': row_idx,
                        'error': error_msg,
                        'id': row.get('telegramID' if self.table_name == 'Users' else self.primary_key)
                    })

                    # Проверяем на массовые одинаковые ошибки
                    if error_msg[:100] == last_error:
                        consecutive_errors += 1
                    else:
                        consecutive_errors = 1
                        last_error = error_msg[:100]

                    # Если слишком много одинаковых ошибок - останавливаем
                    if consecutive_errors > 50:
                        logger.error(f"Too many identical errors ({consecutive_errors}), stopping import")
                        break

            # Коммитим результаты
            if not dry_run:
                if results['errors']:
                    logger.warning(f"Import completed with {len(results['errors'])} errors")
                    # В safe режиме коммитим успешные изменения даже при наличии ошибок
                    session.commit()
                else:
                    session.commit()
                    logger.info(f"Import completed successfully")

                logger.info(f"Results: {results['added']} added, {results['updated']} updated, {results['skipped']} skipped, {len(results['errors'])} errors")
            else:
                logger.info(f"Dry run completed, no changes applied")

        except Exception as e:
            session.rollback()
            logger.error(f"Import failed for {self.table_name}: {e}")
            results['errors'].append({
                'row': 0,
                'error': f"Critical error: {str(e)}"
            })

        return results

    def _process_row(self, session: Session, row: Dict, row_idx: int, dry_run: bool) -> Dict:
        """Обрабатывает одну строку из Google Sheets"""

        try:
            if self.table_name == 'Users':
                # Для Users ищем по telegramID, а не по userID!
                telegram_id = row.get('telegramID')
                if not telegram_id:
                    return {'action': 'skip'}

                # Ищем существующего пользователя по telegramID
                record = session.query(self.model).filter_by(telegramID=telegram_id).first()
            else:
                # Для остальных таблиц используем primary_key
                record_id = row.get(self.primary_key)
                if not record_id:
                    return {'action': 'skip'}

                record = session.query(self.model).filter(
                    getattr(self.model, self.primary_key) == record_id
                ).first()

            if record:
                # Обновляем существующую запись
                changes = self._update_record(session, record, row, row_idx, dry_run)
                if changes:
                    return {'action': 'update', 'changes': changes}
                else:
                    return {'action': 'skip'}
            else:
                # Создаем новую запись
                if self._create_record(session, row, row_idx, dry_run):
                    return {'action': 'add'}
                else:
                    return {'action': 'skip'}

        except Exception as e:
            logger.error(f"Row {row_idx} processing error: {e}")
            # Возвращаем ошибку для включения в отчет
            return {'action': 'error', 'error': str(e)[:200]}

    def _update_record(self, session: Session, record: Any, row: Dict, row_idx: int, dry_run: bool) -> List[Dict]:
        """Обновляет существующую запись"""
        changes = []

        # ПРОВЕРЯЕМ readonly поля на попытку изменения
        for field_name in self.config['readonly_fields']:
            if field_name not in row:
                continue

            # Для Users проверяем userID
            if self.table_name == 'Users' and field_name == 'userID':
                sheet_user_id = row.get('userID')
                db_user_id = getattr(record, 'userID')
                if sheet_user_id and sheet_user_id != db_user_id:
                    # Это критическая ошибка!
                    raise ValueError(
                        f"Attempting to change readonly userID: "
                        f"DB={db_user_id}, sheet={sheet_user_id} for telegramID={record.telegramID}"
                    )

            # telegramID тоже нельзя менять
            elif field_name == 'telegramID' and self.table_name == 'Users':
                sheet_tid = row.get('telegramID')
                db_tid = getattr(record, 'telegramID')
                if sheet_tid != db_tid:
                    raise ValueError(
                        f"Attempting to change telegramID: "
                        f"DB={db_tid}, sheet={sheet_tid}"
                    )

            # Балансы только через транзакции
            elif field_name in ['balanceActive', 'balancePassive']:
                sheet_value = row.get(field_name)
                db_value = getattr(record, field_name)
                if sheet_value is not None:
                    try:
                        sheet_value = float(sheet_value)
                        if abs(sheet_value - db_value) > 0.01:
                            logger.warning(
                                f"Row {row_idx}: Balance mismatch in {field_name}: "
                                f"DB={db_value}, sheet={sheet_value}. Balances can only be changed through transactions!"
                            )
                    except:
                        pass

        # Обновляем только editable_fields
        for field_name in self.config['editable_fields']:
            if field_name not in row:
                continue

            try:
                new_value = self._convert_value(field_name, row[field_name])
            except Exception as e:
                logger.warning(f"Row {row_idx}: Failed to convert {field_name}={row[field_name]}: {e}")
                continue  # Пропускаем битое поле

            old_value = getattr(record, field_name)

            # Специальная обработка для upline
            if field_name == 'upline' and self.table_name == 'Users':
                new_value = validate_upliner(
                    new_value,
                    getattr(record, 'telegramID'),
                    session
                )

            # Проверяем изменилось ли значение
            if self._values_differ(old_value, new_value):
                # Валидация foreign key
                if field_name in self.config.get('foreign_keys', {}):
                    if not validate_foreign_key(self.table_name, field_name, new_value, session):
                        logger.warning(f"Row {row_idx}: Invalid foreign key {field_name}={new_value}")
                        continue

                changes.append({
                    'field': field_name,
                    'old': old_value,
                    'new': new_value
                })

                if not dry_run:
                    setattr(record, field_name, new_value)

        return changes

    def _create_record(self, session: Session, row: Dict, row_idx: int, dry_run: bool) -> bool:
        """Создает новую запись"""

        # Проверяем required fields
        for field in self.config['required_fields']:
            if field not in row or not row[field]:
                logger.warning(f"Row {row_idx}: Missing required field: {field}")
                return False

        # Для Users проверяем дубликат telegramID
        if self.table_name == 'Users':
            telegram_id = row.get('telegramID')
            existing = session.query(self.model).filter_by(telegramID=telegram_id).first()
            if existing:
                logger.error(
                    f"Row {row_idx}: telegramID {telegram_id} already exists "
                    f"with userID={existing.userID}, cannot add new userID={row.get('userID')}"
                )
                return False

        if dry_run:
            return True

        record = self.model()

        # Заполняем ВСЕ поля для новой записи
        for field_name, value in row.items():
            # Пропускаем поля, которых нет в модели
            if not hasattr(record, field_name):
                continue

            # userID для Users автогенерируется, не берем из таблицы
            if self.table_name == 'Users' and field_name == 'userID':
                continue

            # createdAt генерируется автоматически
            if field_name == 'createdAt':
                continue

            # Конвертируем значение с обработкой ошибок
            try:
                converted_value = self._convert_value(field_name, value)
            except Exception as e:
                logger.warning(f"Row {row_idx}: Failed to convert {field_name}={value}: {e}, using None")
                converted_value = None

            # Специальная обработка для upline при создании
            if field_name == 'upline' and self.table_name == 'Users':
                telegram_id = row.get('telegramID')
                if converted_value == telegram_id:
                    converted_value = config.DEFAULT_REFERRER_ID
                elif not converted_value:
                    converted_value = config.DEFAULT_REFERRER_ID

            setattr(record, field_name, converted_value)

        session.add(record)
        return True

    def _convert_value(self, field_name: str, value: Any) -> Any:
        """
        Преобразует значение в нужный тип
        ПРИНЦИП: ошибка лучше чем потеря данных
        """

        # Обработка пустых значений
        if value in [None, '', 'None', 'NULL', 'null', 'Null']:
            # Для upline пустое значение = ОШИБКА (будет обработано в validate_upliner)
            if field_name == 'upline' and self.table_name == 'Users':
                raise ValueError(f"Empty upline is not allowed")
            return None

        # Получаем валидатор для поля
        validators = self.config.get('field_validators', {})
        validator = validators.get(field_name)

        # Специальная обработка для дат БЕЗ валидатора
        if field_name in ['lastActive', 'createdAt', 'confirmationTime', 'birthday'] and not validator:
            return self._parse_date(value)

        # Если валидатора нет - возвращаем как есть
        if not validator:
            if isinstance(value, str):
                value = value.strip()
                return value if value else None
            return value

        # ОБРАБОТКА ПО ВАЛИДАТОРАМ

        if validator == 'email':
            if not value:
                return None
            return str(value).lower().strip()

        elif validator == 'phone':
            # НЕ ТРОГАЕМ
            if not value:
                return None
            return str(value).strip()

        elif validator == 'boolean':
            # Всегда 0 или 1 в БД
            if isinstance(value, bool):
                return 1 if value else 0
            if isinstance(value, (int, float)):
                return 1 if value else 0
            if isinstance(value, str):
                val = value.lower().strip()
                if val in ('true', '1', 'yes'):
                    return 1
                else:
                    return 0
            return 0

        elif validator == 'int':
            try:
                if isinstance(value, str):
                    value = value.strip().replace(',', '.').replace(' ', '')
                return int(float(value))
            except:
                raise ValueError(f"Cannot convert {field_name}={value} to int")

        elif validator == 'float':
            try:
                if isinstance(value, str):
                    value = value.strip().replace(',', '.').replace(' ', '')
                return float(value)
            except:
                raise ValueError(f"Cannot convert {field_name}={value} to float")

        elif validator == 'float_or_empty':
            # Только для ActiveBalance
            if not value or value == '':
                return 0.0
            try:
                if isinstance(value, str):
                    value = value.strip().replace(',', '.').replace(' ', '')
                return float(value)
            except:
                raise ValueError(f"Cannot convert {field_name}={value} to float")

        elif validator == 'date' or validator == 'datetime':
            result = self._parse_date(value)
            if result is None and value:  # Если есть значение но не распарсилось
                raise ValueError(f"Invalid date format for {field_name}={value}")
            return result

        elif validator == 'special_upliner':
            # Конвертируем upline в int
            try:
                return int(float(str(value).strip()))
            except:
                raise ValueError(f"Invalid upline format: {value}")

        elif isinstance(validator, list):
            # Список допустимых значений
            if value not in validator:
                raise ValueError(f"Value '{value}' not allowed for {field_name}. Must be one of: {validator}")
            return value

        else:
            # Неизвестный валидатор
            if isinstance(value, str):
                value = value.strip()
                return value if value else None
            return value

    def _parse_date(self, value: Any) -> Optional[datetime]:
        """
        Парсит дату ТОЛЬКО в форматах нашего кода
        """
        if not value:
            return None

        if isinstance(value, datetime):
            return value

        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None

            # ТОЛЬКО наши форматы
            formats = [
                "%Y-%m-%d %H:%M:%S.%f",  # 2025-01-30 11:50:00.000000
                "%Y-%m-%d %H:%M:%S",  # 2025-01-30 11:50:00
                "%Y-%m-%d",  # 2025-01-30
            ]

            for fmt in formats:
                try:
                    return datetime.strptime(value, fmt)
                except ValueError:
                    continue

            # Не смогли распарсить - возвращаем None (ошибка будет выше)
            return None

        return None

    def _values_differ(self, old_value: Any, new_value: Any) -> bool:
        """Проверяет отличаются ли значения"""

        # Нормализация для сравнения
        if old_value is None and new_value in ['', None]:
            return False
        if new_value is None and old_value in ['', None]:
            return False

        if isinstance(old_value, bool):
            return bool(old_value) != bool(new_value)

        if isinstance(old_value, (int, float)) and new_value is not None:
            try:
                return abs(float(old_value) - float(new_value)) > 0.001
            except:
                return True

        if isinstance(old_value, datetime) and isinstance(new_value, datetime):
            return old_value != new_value

        # Строковое сравнение
        old_str = str(old_value).strip() if old_value not in [None, ''] else ''
        new_str = str(new_value).strip() if new_value not in [None, ''] else ''

        return old_str != new_str