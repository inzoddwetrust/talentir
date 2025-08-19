import os
import json
import logging
from datetime import datetime
from typing import Dict, Any, Optional
from dataclasses import dataclass, field
from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError
from google_services import get_google_services
from database import User, Project, Option, Purchase, Payment, Bonus, Transfer, ActiveBalance, PassiveBalance
from init import Session
import config

logger = logging.getLogger(__name__)


@dataclass
class ImportStats:
    total: int = 0
    updated: int = 0
    added: int = 0
    skipped: int = 0
    errors: int = 0
    error_rows: list = field(default_factory=list)

    def add_error(self, row: int, error: str):
        self.errors += 1
        self.error_rows.append((row, error))

    def get_report(self) -> str:
        report = [
            f"Import statistics:",
            f"Total rows: {self.total}",
            f"Updated: {self.updated}",
            f"Added: {self.added}",
            f"Skipped: {self.skipped}",
            f"Errors: {self.errors}"
        ]

        if self.error_rows:
            report.append("\nErrors:")
            for row, error in self.error_rows:
                report.append(f"Row {row}: {error}")

        return "\n".join(report)


class DataUtils:
    """Утилиты для работы с данными"""

    @staticmethod
    def parse_date(value: str, format: str = "%Y-%m-%d %H:%M:%S") -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.strptime(value.strip(), format)
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def parse_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.lower() in ('true', '1', 'yes', 'y', 't')
        return False

    @staticmethod
    def parse_float(value: Any) -> Optional[float]:
        if value == '' or value is None:
            return None
        try:
            result = float(value)
            return result
        except (ValueError, TypeError):
            return None

    @staticmethod
    def parse_int(value: Any) -> Optional[int]:
        if not value:
            return None
        try:
            return int(float(value)) if float(value) >= 0 else None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def clean_str(value: Any) -> Optional[str]:
        if value is None:
            return None
        cleaned = str(value).strip()
        return cleaned if cleaned else None


class BaseImporter:
    """Базовый импортер с общей логикой"""

    # Обязательные поля для проверки
    REQUIRED_FIELDS = []

    def __init__(self):
        self.stats = ImportStats()
        self.utils = DataUtils()

    def validate_required_fields(self, row: Dict[str, Any], row_num: int) -> bool:
        """Проверка обязательных полей"""
        for field in self.REQUIRED_FIELDS:
            if not row.get(field):
                self.stats.add_error(row_num, f"Missing required field: {field}")
                return False
        return True

    def validate_row(self, row: Dict[str, Any], row_num: int) -> bool:
        return self.validate_required_fields(row, row_num)

    async def import_sheet(self, sheet) -> ImportStats:
        """Основной метод импорта с обработкой ошибок"""
        rows = sheet.get_all_records()
        self.stats.total = len(rows)

        with Session() as session:
            for idx, row in enumerate(rows, start=2):
                try:
                    if not self.validate_row(row, idx):
                        self.stats.skipped += 1
                        continue

                    if self.process_row(row, session):
                        self.stats.updated += 1
                    else:
                        self.stats.added += 1

                    # Коммитим каждые 50 записей для минимизации потерь при ошибке
                    if (idx - 1) % 50 == 0:
                        try:
                            session.commit()
                        except IntegrityError as e:
                            session.rollback()
                            self.stats.add_error(idx, f"Integrity error during batch commit: {str(e)}")
                            continue

                except IntegrityError as e:
                    session.rollback()  # Откатываем только текущую транзакцию

                    # Специальная обработка для разных типов ошибок целостности
                    error_str = str(e)
                    if 'UNIQUE constraint failed: users.telegramID' in error_str:
                        self.stats.add_error(idx, f"Duplicate telegramID: {row.get('telegramID')}")
                        self.stats.skipped += 1
                        logger.warning(f"Row {idx}: Skipping duplicate telegramID {row.get('telegramID')}")
                    elif 'UNIQUE constraint failed' in error_str:
                        self.stats.add_error(idx, f"Duplicate record: {error_str}")
                        self.stats.skipped += 1
                    elif 'FOREIGN KEY constraint failed' in error_str:
                        self.stats.add_error(idx, f"Foreign key error: {error_str}")
                        self.stats.skipped += 1
                    else:
                        self.stats.add_error(idx, str(e))
                    continue

                except Exception as e:
                    session.rollback()
                    self.stats.add_error(idx, str(e))
                    logger.error(f"Row {idx} error: {e}", exc_info=True)
                    continue

            # Финальный коммит оставшихся записей
            try:
                session.commit()
            except IntegrityError as e:
                session.rollback()
                logger.error(f"Final commit failed: {e}")
                self.stats.add_error(0, f"Final commit error: {str(e)}")
            except Exception as e:
                session.rollback()
                logger.error(f"Import failed during final commit: {e}", exc_info=True)
                self.stats.add_error(0, f"Import error: {str(e)}")

        return self.stats


class ConfigImporter:
    """Класс для импорта конфигурационных переменных из Google Sheets"""

    @staticmethod
    async def import_config() -> Dict[str, Any]:
        """
        Импортирует конфигурацию из листа Google Sheets

        Returns:
            Dict[str, Any]: Словарь с настройками
        """
        try:
            sheets_client, _ = get_google_services()
            sheet = sheets_client.open_by_key(config.GOOGLE_SHEET_ID).worksheet("Config")

            # Получаем все записи
            records = sheet.get_all_records()

            if not records:
                logger.warning("Config sheet is empty or has no valid records")
                return {}

            utils = DataUtils()

            # Создаем словарь с настройками
            config_dict = {}
            for record in records:
                if 'key' not in record or 'value' not in record:
                    logger.warning(f"Invalid config record: {record}")
                    continue

                key = record['key'].strip()
                value = record['value']

                if not key:
                    continue

                try:
                    # Пробуем распарсить как JSON для сложных структур
                    if isinstance(value, str) and (value.startswith('{') or value.startswith('[')):
                        value = json.loads(value)
                    # Пробуем преобразовать числовые значения
                    elif isinstance(value, str):
                        if value.lower() == 'true':
                            value = True
                        elif value.lower() == 'false':
                            value = False
                        else:
                            try:
                                if '.' in value:
                                    value = float(value)
                                else:
                                    value = int(value)
                            except ValueError:
                                # Оставляем как строку
                                pass
                except json.JSONDecodeError:
                    logger.warning(f"Failed to parse JSON for key {key}: {value}")

                config_dict[key] = value

            logger.info(f"Imported {len(config_dict)} config variables")
            return config_dict

        except Exception as e:
            logger.error(f"Error importing config: {e}")
            return {}

    @staticmethod
    def get_nested_value(config_dict: Dict[str, Any], key_path: str, default: Any = None) -> Any:
        """
        Получает значение из конфигурации по пути ключей

        Args:
            config_dict: Словарь с конфигурацией
            key_path: Путь к значению (например, 'SOCIAL_LINKS.telegram_link')
            default: Значение по умолчанию, если ключ не найден

        Returns:
            Any: Значение из конфигурации или значение по умолчанию
        """
        keys = key_path.split('.')
        value = config_dict

        try:
            for k in keys:
                if isinstance(value, dict):
                    value = value[k]
                else:
                    return default
            return value
        except (KeyError, TypeError):
            return default

    @staticmethod
    def update_config_module(config_dict: Dict[str, Any]) -> None:
        """
        Обновляет переменные в модуле config

        Args:
            config_dict: Словарь с конфигурацией
        """
        # Список переменных config, которые можно обновлять
        updateable_vars = [
            'PURCHASE_BONUSES', 'STRATEGY_COEFFICIENTS', 'TRANSFER_BONUS',
            'SOCIAL_LINKS', 'FAQ_URL', 'REQUIRED_CHANNELS', 'PROJECT_DOCUMENTS'
        ]

        for var_name in updateable_vars:
            if var_name in config_dict:
                setattr(config, var_name, config_dict[var_name])
                logger.info(f"Updated config.{var_name}")


class ProjectImporter(BaseImporter):
    REQUIRED_FIELDS = ['projectID', 'projectName', 'lang', 'projectTitle', 'status']

    def __init__(self):
        super().__init__()
        self.doc_temp_dir = "doc_temp"
        os.makedirs(self.doc_temp_dir, exist_ok=True)

    def process_row(self, row: Dict[str, Any], session) -> bool:
        project = session.query(Project).filter(
            and_(
                Project.projectID == row['projectID'],
                Project.lang == row['lang']
            )
        ).first()

        is_update = bool(project)
        if not project:
            project = Project()

        project.projectID = row['projectID']
        project.lang = row['lang']
        project.projectName = row['projectName']
        project.projectTitle = row['projectTitle']
        project.fullText = row.get('fullText')
        project.status = row['status']
        project.rate = self.utils.parse_float(row.get('rate'))
        project.linkImage = self.utils.clean_str(row.get('linkImage'))
        project.linkPres = self.utils.clean_str(row.get('linkPres'))
        project.linkVideo = self.utils.clean_str(row.get('linkVideo'))

        # Просто сохраняем слаг из docsFolder, если он есть
        project.docsFolder = self.utils.clean_str(row.get('docsFolder'))

        if not is_update:
            session.add(project)

        return is_update


class UserImporter(BaseImporter):
    REQUIRED_FIELDS = ['userID', 'telegramID']

    def process_row(self, row: Dict[str, Any], session) -> bool:
        # ВАЖНО: Ищем пользователя по telegramID (он уникальный), а не по userID
        user = session.query(User).filter_by(telegramID=row['telegramID']).first()
        is_update = bool(user)

        if not user:
            user = User()
            # userID устанавливаем только при создании нового пользователя
            user.userID = row['userID']

        # telegramID не меняем если пользователь уже существует
        if not is_update:
            user.telegramID = row['telegramID']

        # Обновляем все остальные поля
        user.createdAt = self.utils.parse_date(row.get('createdAt'))
        user.upline = self.utils.parse_int(row.get('upline'))
        user.lang = self.utils.clean_str(row.get('lang'))
        user.firstname = self.utils.clean_str(row.get('firstname'))
        user.surname = self.utils.clean_str(row.get('surname'))
        user.birthday = self.utils.parse_date(row.get('birthday'), "%Y-%m-%d")
        user.address = self.utils.clean_str(row.get('address'))
        user.phoneNumber = self.utils.clean_str(row.get('phoneNumber'))
        user.city = self.utils.clean_str(row.get('city'))
        user.country = self.utils.clean_str(row.get('country'))
        user.email = self.utils.clean_str(row.get('email'))
        user.balanceActive = self.utils.parse_float(row.get('balanceActive')) or 0.0
        user.balancePassive = self.utils.parse_float(row.get('balancePassive')) or 0.0
        user.isFilled = self.utils.parse_bool(row.get('isFilled'))
        user.kyc = self.utils.parse_bool(row.get('kyc'))
        user.lastActive = self.utils.parse_date(row.get('lastActive'))
        user.status = self.utils.clean_str(row.get('status')) or 'active'
        user.notes = self.utils.clean_str(row.get('notes'))
        user.settings = self.utils.clean_str(row.get('settings'))

        if not is_update:
            session.add(user)

        return is_update


class OptionImporter(BaseImporter):
    REQUIRED_FIELDS = ['optionID', 'projectID', 'projectName']

    def validate_row(self, row: Dict[str, Any], row_num: int) -> bool:
        if not super().validate_row(row, row_num):
            return False

        if not self.utils.parse_float(row.get('costPerShare')):
            self.stats.add_error(row_num, "Invalid costPerShare")
            return False

        if not self.utils.parse_int(row.get('packQty')):
            self.stats.add_error(row_num, "Invalid packQty")
            return False

        return True

    def process_row(self, row: Dict[str, Any], session) -> bool:
        option = session.query(Option).filter_by(optionID=row['optionID']).first()
        is_update = bool(option)

        if not option:
            option = Option()

        option.optionID = row['optionID']
        option.projectID = row['projectID']
        option.projectName = row['projectName']
        option.costPerShare = self.utils.parse_float(row['costPerShare'])
        option.packQty = self.utils.parse_int(row['packQty'])
        option.packPrice = self.utils.parse_float(row['packPrice'])
        option.isActive = self.utils.parse_bool(row.get('isActive?', True))

        if not is_update:
            session.add(option)

        return is_update


class PaymentImporter(BaseImporter):
    REQUIRED_FIELDS = ['paymentID', 'userID', 'firstname', 'amount', 'method', 'sumCurrency', 'status', 'direction']

    def process_row(self, row: Dict[str, Any], session) -> bool:
        payment = session.query(Payment).filter_by(paymentID=row['paymentID']).first()
        is_update = bool(payment)

        if not payment:
            payment = Payment()

        payment.paymentID = row['paymentID']
        payment.createdAt = self.utils.parse_date(row.get('createdAt'))
        payment.userID = row['userID']
        payment.firstname = row['firstname']
        payment.surname = self.utils.clean_str(row.get('surname'))
        payment.direction = row.get('direction', 'incoming')  # Default to 'incoming' if not specified
        payment.amount = self.utils.parse_float(row['amount'])
        payment.method = row['method']
        payment.fromWallet = self.utils.clean_str(row.get('fromWallet'))
        payment.toWallet = self.utils.clean_str(row.get('toWallet'))
        payment.txid = self.utils.clean_str(row.get('txid'))
        payment.sumCurrency = row['sumCurrency']
        payment.status = row['status']
        payment.confirmedBy = self.utils.clean_str(row.get('confirmedBy'))
        payment.confirmationTime = self.utils.parse_date(row.get('confirmationTime'))

        if not is_update:
            session.add(payment)

        return is_update


class ActiveBalanceImporter(BaseImporter):
    REQUIRED_FIELDS = ['paymentID', 'userID', 'firstname', 'status', 'reason']

    def validate_row(self, row: Dict[str, Any], row_num: int) -> bool:
        # Для manual_addition записей особая валидация
        if 'manual_addition' in row.get('reason', ''):
            # Проверяем все поля кроме amount (он может быть пустым или 0)
            required_fields = ['paymentID', 'userID', 'firstname', 'status', 'reason']
            for field in required_fields:
                if not row.get(field):
                    self.stats.add_error(row_num, f"Missing required field: {field}")
                    return False
            return True
        else:
            # Для остальных записей стандартная проверка включая amount
            required_with_amount = self.REQUIRED_FIELDS + ['amount']
            for field in required_with_amount:
                if not row.get(field):
                    self.stats.add_error(row_num, f"Missing required field: {field}")
                    return False
            return True

    def process_row(self, row: Dict[str, Any], session) -> bool:
        record = session.query(ActiveBalance).filter_by(paymentID=row['paymentID']).first()
        is_update = bool(record)

        if not record:
            record = ActiveBalance()

        # Особая обработка для manual_addition записей
        if 'manual_addition' in row.get('reason', ''):
            # Для manual_addition amount может быть пустым или 0
            amount = self.utils.parse_float(row.get('amount', 0))
            if amount is None:  # Если не удалось распарсить, ставим 0
                amount = 0.0
        else:
            # Для остальных записей amount обязателен
            amount = self.utils.parse_float(row['amount'])
            if amount is None:
                # Не должно произойти, так как мы проверили в validate_row
                return False

        record.paymentID = row['paymentID']
        record.createdAt = self.utils.parse_date(row.get('createdAt'))
        record.userID = row['userID']
        record.firstname = row['firstname']
        record.surname = self.utils.clean_str(row.get('surname'))
        record.amount = amount  # Используем обработанное значение
        record.status = row['status']
        record.reason = row['reason']
        record.link = self.utils.clean_str(row.get('link', ''))
        record.notes = self.utils.clean_str(row.get('notes'))

        if not is_update:
            session.add(record)

        return is_update


class PassiveBalanceImporter(BaseImporter):
    REQUIRED_FIELDS = ['paymentID', 'userID', 'firstname', 'amount', 'status', 'reason']

    def process_row(self, row: Dict[str, Any], session) -> bool:
        record = session.query(PassiveBalance).filter_by(paymentID=row['paymentID']).first()
        is_update = bool(record)

        if not record:
            record = PassiveBalance()

        record.paymentID = row['paymentID']
        record.createdAt = self.utils.parse_date(row.get('createdAt'))
        record.userID = row['userID']
        record.firstname = row['firstname']
        record.surname = self.utils.clean_str(row.get('surname'))
        record.amount = self.utils.parse_float(row['amount'])
        record.status = row['status']
        record.reason = row['reason']
        record.link = self.utils.clean_str(row.get('link'))
        record.notes = self.utils.clean_str(row.get('notes'))

        if not is_update:
            session.add(record)

        return is_update


class TransferImporter(BaseImporter):
    REQUIRED_FIELDS = ['transferID', 'senderUserID', 'senderFirstname', 'fromBalance',
                       'amount', 'recieverUserID', 'receiverFirstname', 'toBalance', 'status']

    def process_row(self, row: Dict[str, Any], session) -> bool:
        transfer = session.query(Transfer).filter_by(transferID=row['transferID']).first()
        is_update = bool(transfer)

        if not transfer:
            transfer = Transfer()

        transfer.transferID = row['transferID']
        transfer.createdAt = self.utils.parse_date(row.get('createdAt'))
        transfer.senderUserID = row['senderUserID']
        transfer.senderFirstname = row['senderFirstname']
        transfer.senderSurname = self.utils.clean_str(row.get('senderSurname'))
        transfer.fromBalance = row['fromBalance']
        transfer.amount = self.utils.parse_float(row['amount'])
        transfer.recieverUserID = row['recieverUserID']
        transfer.receiverFirstname = row['receiverFirstname']
        transfer.receiverSurname = self.utils.clean_str(row.get('receiverSurname'))
        transfer.toBalance = row['toBalance']
        transfer.status = row['status']
        transfer.notes = self.utils.clean_str(row.get('notes'))

        if not is_update:
            session.add(transfer)

        return is_update


class PurchaseImporter(BaseImporter):
    REQUIRED_FIELDS = [
        'purchaseID', 'userID', 'projectID', 'projectName',
        'optionID', 'packQty', 'packPrice'
    ]

    def process_row(self, row: Dict[str, Any], session) -> bool:
        purchase = session.query(Purchase).filter_by(purchaseID=row['purchaseID']).first()
        is_update = bool(purchase)

        if not purchase:
            purchase = Purchase()

        purchase.purchaseID = row['purchaseID']
        purchase.createdAt = self.utils.parse_date(row.get('createdAt'))
        purchase.userID = row['userID']
        purchase.projectID = row['projectID']
        purchase.projectName = row['projectName']
        purchase.optionID = row['optionID']
        purchase.packQty = self.utils.parse_int(row['packQty'])
        purchase.packPrice = self.utils.parse_float(row['packPrice'])

        if not is_update:
            session.add(purchase)

        return is_update


class BonusImporter(BaseImporter):
    REQUIRED_FIELDS = [
        'bonusID', 'userID', 'bonusRate', 'bonusAmount'
    ]

    def process_row(self, row: Dict[str, Any], session) -> bool:
        bonus = session.query(Bonus).filter_by(bonusID=row['bonusID']).first()
        is_update = bool(bonus)

        if not bonus:
            bonus = Bonus()

        bonus.bonusID = row['bonusID']
        bonus.createdAt = self.utils.parse_date(row.get('createdAt'))

        # Основные поля
        bonus.userID = row['userID']
        bonus.downlineID = self.utils.parse_int(row.get('downlineID'))
        bonus.purchaseID = self.utils.parse_int(row.get('purchaseID'))

        # Данные о покупке
        bonus.projectID = self.utils.parse_int(row.get('projectID'))
        bonus.optionID = self.utils.parse_int(row.get('optionID'))
        bonus.packQty = self.utils.parse_int(row.get('packQty'))
        bonus.packPrice = self.utils.parse_float(row.get('packPrice'))

        # Данные о бонусе
        bonus.uplineLevel = self.utils.parse_int(row.get('uplineLevel'))
        bonus.bonusRate = self.utils.parse_float(row['bonusRate'])
        bonus.bonusAmount = self.utils.parse_float(row['bonusAmount'])

        # Статус и заметки
        bonus.status = self.utils.clean_str(row.get('status')) or 'pending'
        bonus.notes = self.utils.clean_str(row.get('notes'))

        if not is_update:
            session.add(bonus)

        return is_update


async def import_all(bot=None):
    """Импорт всех данных из Google Sheets"""
    sheets_client, _ = get_google_services()
    spreadsheet = sheets_client.open_by_key(config.GOOGLE_SHEET_ID)

    importers = {
        'Users': UserImporter(),
        'Projects': ProjectImporter(),
        'Options': OptionImporter(),
        'Payments': PaymentImporter(),
        'Purchases': PurchaseImporter(),
        'Bonuses': BonusImporter(),
        'ActiveBalance': ActiveBalanceImporter(),
        'PassiveBalance': PassiveBalanceImporter(),
        'Transfers': TransferImporter(),
    }

    results = {}

    for sheet_name, importer in importers.items():
        try:
            sheet = spreadsheet.worksheet(sheet_name)
            stats = await importer.import_sheet(sheet)
            results[sheet_name] = stats

            if bot and stats.errors > 0:
                error_report = f"Import report for {sheet_name}:\n{stats.get_report()}"
                for admin_id in config.ADMINS:
                    try:
                        await bot.send_message(admin_id, error_report)
                    except Exception as e:
                        logger.error(f"Failed to send report to admin {admin_id}: {e}")

        except Exception as e:
            logger.error(f"Failed to import {sheet_name}: {e}", exc_info=True)
            results[sheet_name] = f"Failed: {str(e)}"

    return results


# Функция для обновления конфигурации для использования в GlobalVariables
async def update_config():
    """Обновляет конфигурацию из Google Sheets"""
    config_dict = await ConfigImporter.import_config()
    ConfigImporter.update_config_module(config_dict)
    return config_dict