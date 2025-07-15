import logging
from typing import List, Dict, Set
from sqlalchemy import MetaData, Table, Column, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateTable
from init import engine, Base

class DatabaseSynchronizer:
    def __init__(self, engine: Engine, base: Base):
        self.engine = engine
        self.base = base
        self.inspector = inspect(engine)
        self.logger = logging.getLogger(__name__)

    def get_model_tables(self) -> Dict[str, List[Column]]:
        """Получает структуру таблиц из моделей"""
        return {
            table.__tablename__: [column.copy() for column in table.__table__.columns]
            for table in self.base._decl_class_registry.values()
            if hasattr(table, '__tablename__')
        }

    def get_database_tables(self) -> Dict[str, List[Dict]]:
        """Получает структуру существующих таблиц из БД"""
        return {
            table_name: self.inspector.get_columns(table_name)
            for table_name in self.inspector.get_table_names()
        }

    def compare_columns(self, model_cols: List[Column], db_cols: List[Dict]) -> Dict:
        """Сравнивает колонки модели и БД"""
        model_col_names = {col.name: col for col in model_cols}
        db_col_names = {col['name']: col for col in db_cols}

        return {
            'new_columns': [
                col for name, col in model_col_names.items()
                if name not in db_col_names
            ],
            'missing_columns': [
                name for name in db_col_names
                if name not in model_col_names
            ],
            'modified_columns': [
                (name, model_col_names[name]) for name in model_col_names
                if name in db_col_names and self._columns_differ(
                    model_col_names[name], db_col_names[name]
                )
            ]
        }

    def _columns_differ(self, model_col: Column, db_col: Dict) -> bool:
        """Проверяет, отличается ли колонка в модели от колонки в БД"""
        # Здесь можно добавить более детальное сравнение типов и атрибутов
        return str(model_col.type) != db_col['type']

    def generate_migrations(self) -> List[str]:
        """Генерирует SQL для синхронизации"""
        model_tables = self.get_model_tables()
        db_tables = self.get_database_tables()
        migrations = []

        # Новые таблицы
        for table_name in set(model_tables) - set(db_tables):
            table = Table(table_name, MetaData(), *model_tables[table_name])
            migrations.append(str(CreateTable(table)))

        # Изменения в существующих таблицах
        for table_name in set(model_tables) & set(db_tables):
            differences = self.compare_columns(
                model_tables[table_name],
                db_tables[table_name]
            )

            # Добавление новых колонок
            for column in differences['new_columns']:
                migrations.append(
                    f"ALTER TABLE {table_name} ADD COLUMN {self._column_to_sql(column)};"
                )

        return migrations

    def _column_to_sql(self, column: Column) -> str:
        """Конвертирует объект Column в SQL-определение"""
        return f"{column.name} {column.type}"

    def synchronize(self, dry_run: bool = True) -> None:
        """Выполняет синхронизацию БД с моделями"""
        migrations = self.generate_migrations()

        if not migrations:
            self.logger.info("База данных актуальна")
            return

        self.logger.info("Найдены следующие изменения:")
        for migration in migrations:
            self.logger.info(migration)

        if not dry_run:
            with self.engine.begin() as conn:
                for migration in migrations:
                    try:
                        conn.execute(migration)
                        self.logger.info(f"Выполнено: {migration}")
                    except Exception as e:
                        self.logger.error(f"Ошибка при выполнении: {migration}")
                        self.logger.error(str(e))
                        raise

def sync_database(dry_run: bool = True):
    """Утилита для синхронизации БД"""
    synchronizer = DatabaseSynchronizer(engine, Base)
    synchronizer.synchronize(dry_run)

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    sync_database(dry_run=True)