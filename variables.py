import asyncio
import logging
import random
from typing import Dict, Any, Callable, List
from datetime import datetime, timedelta
from sqlalchemy import func
from database import User, Project, Purchase
from init import Session
import config
from imports import update_config
from crypto_rates import fetch_from_binance, fetch_from_coingecko

logger = logging.getLogger(__name__)


class GlobalVariables:
    """
    Менеджер глобальных переменных проекта.
    Действует как синглтон, периодически обновляет значения.
    """
    _instance = None
    _variables: Dict[str, Any] = {}
    _update_intervals: Dict[str, int] = {}
    _last_updates: Dict[str, datetime] = {}
    _update_functions: Dict[str, Callable] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(GlobalVariables, cls).__new__(cls)
        return cls._instance

    @classmethod
    async def get(cls, key: str) -> Any:
        """Получение значения с проверкой необходимости обновления"""
        instance = cls()
        if key not in cls._variables:
            await instance._update_variable(key)
        elif key in cls._update_intervals:
            now = datetime.utcnow()
            if (now - cls._last_updates[key]).total_seconds() > cls._update_intervals[key]:
                await instance._update_variable(key)
        return cls._variables.get(key)

    @classmethod
    def register_variable(cls, key: str, update_func: Callable, interval: int = 300):
        """Регистрация новой переменной с функцией обновления и интервалом"""
        cls._update_functions[key] = update_func
        cls._update_intervals[key] = interval
        logger.info(f"Registered global variable: {key} with {interval}s update interval")

    @classmethod
    def set_static_variable(cls, key: str, value: Any):
        """Установка статической переменной, которая не требует обновления"""
        cls._variables[key] = value
        cls._last_updates[key] = datetime.utcnow()

    async def _update_variable(self, key: str):
        """Обновление значения переменной"""
        if key in self._update_functions:
            try:
                self._variables[key] = await self._update_functions[key]()
                self._last_updates[key] = datetime.utcnow()
                logger.debug(f"Updated global variable: {key} = {self._variables[key]}")
            except Exception as e:
                logger.error(f"Error updating variable {key}: {e}")

    async def start_update_loop(self):
        """Запуск цикла обновления переменных"""
        logger.info("Starting global variables update loop")
        while True:
            try:
                for key in self._update_functions.keys():
                    # Пропускаем переменные, которые были установлены через set_static_variable
                    if key not in self._update_intervals:
                        continue
                    await self._update_variable(key)
            except Exception as e:
                logger.error(f"Error in update loop: {e}")
            await asyncio.sleep(min(self._update_intervals.values()))

    @property
    def variables(self):
        return self._variables


# Функции обновления статистики
async def update_users_count():
    """Общее количество пользователей"""
    with Session() as session:
        return session.query(func.count(User.userID)).scalar()


async def update_purchases_total():
    """Общая сумма инвестиций"""
    with Session() as session:
        return float(session.query(func.sum(Purchase.packPrice)).scalar() or 0)


async def update_projects_count():
    """Количество уникальных проектов"""
    with Session() as session:
        return session.query(func.count(func.distinct(Project.projectID))).scalar()


async def update_active_users():
    """Количество активных пользователей за последние 24 часа"""
    with Session() as session:
        yesterday = datetime.utcnow() - timedelta(days=1)
        return session.query(func.count(User.userID)).filter(
            User.lastActive >= yesterday
        ).scalar()


# Функции обновления криптовалютных курсов
async def update_crypto_rates():
    """Обновление курсов криптовалют"""
    rates = await fetch_from_binance()
    if rates is None:
        rates = await fetch_from_coingecko()
    return rates


# Функции обновления конфигурации
async def update_wallets():
    """Адреса кошельков"""
    return config.WALLETS.copy()


async def update_admins():
    """Список администраторов"""
    return config.ADMINS.copy()


async def update_admin_links():
    """Контакты администраторов"""
    return config.ADMIN_LINKS.copy()


async def update_sorted_projects() -> List[int]:
    """Возвращает список project_ids, отсортированный по рейтингу, только для активных проектов"""
    with Session() as session:

        projects = session.query(
            Project.projectID,
            func.coalesce(Project.rate, 999).label('effective_rate')
        ).filter(
            Project.status.in_(["active", "child"])  # Фильтруем активные и child проекты
        ).distinct().all()

        # Группируем проекты по рейтингу
        rating_groups = {}
        for pid, rate in projects:
            if rate not in rating_groups:
                rating_groups[rate] = []
            rating_groups[rate].append(pid)

        # Перемешиваем проекты с одинаковым рейтингом и формируем финальный список
        sorted_projects = []
        for rate in sorted(rating_groups.keys()):  # Сортируем по возрастанию рейтинга
            project_ids = rating_groups[rate]
            random.shuffle(project_ids)  # Случайный порядок для проектов с одинаковым рейтингом
            sorted_projects.extend(project_ids)

        return sorted_projects


def initialize_variables():
    """Инициализация менеджера глобальных переменных"""
    global_vars = GlobalVariables()

    # Статистика проекта
    global_vars.register_variable('usersCount', update_users_count, 300)
    global_vars.register_variable('purchasesTotal', update_purchases_total, 100)
    global_vars.register_variable('projectsCount', update_projects_count, 3000)
    global_vars.register_variable('active_users', update_active_users, 300)

    # Криптовалютные курсы
    global_vars.register_variable('crypto_rates', update_crypto_rates, 300)

    # Отсортированный список проектов
    global_vars.register_variable('sorted_projects', update_sorted_projects, 3600)

    # Системная конфигурация
    global_vars.register_variable('wallets', update_wallets, 3600)
    global_vars.register_variable('admins', update_admins, 3600)
    global_vars.register_variable('admin_links', update_admin_links, 3600)

    # Добавляем переменные из config, которые можно обновлять из Google Sheets
    global_vars.set_static_variable('purchase_bonuses', config.PURCHASE_BONUSES)
    global_vars.set_static_variable('strategy_coefficients', config.STRATEGY_COEFFICIENTS)
    global_vars.set_static_variable('transfer_bonus', config.TRANSFER_BONUS)
    global_vars.set_static_variable('social_links', config.SOCIAL_LINKS)
    global_vars.set_static_variable('faq_url', config.FAQ_URL)
    global_vars.set_static_variable('required_channels', config.REQUIRED_CHANNELS)
    global_vars.set_static_variable('project_documents', config.PROJECT_DOCUMENTS)

    # Добавляем функцию обновления конфигурации
    global_vars.register_variable('config', update_config, 3600)  # Обновляем каждый час

    return global_vars
