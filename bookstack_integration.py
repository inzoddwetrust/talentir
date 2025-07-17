import logging
import tempfile
import jinja2
import requests
import os
from bs4 import BeautifulSoup
from datetime import datetime
from typing import Optional, Dict, Any

import config
from bookstack_client import BookStackClient, BookStackAPIError

logger = logging.getLogger(__name__)


class TemplateCache:
    """Класс для кеширования HTML-шаблонов из BookStack"""
    _cache = {}  # {key: (html, timestamp)}
    _ttl = 600

    @classmethod
    def get(cls, key: str) -> Optional[str]:
        """Получение HTML из кеша с проверкой TTL"""
        if key in cls._cache:
            html, timestamp = cls._cache[key]
            if (datetime.utcnow() - timestamp).total_seconds() < cls._ttl:
                return html
        return None

    @classmethod
    def set(cls, key: str, html: str) -> None:
        """Сохранение HTML в кеш"""
        cls._cache[key] = (html, datetime.utcnow())

    @classmethod
    def clear(cls):
        """Очистка кеша"""
        cls._cache.clear()


class BookStackManager:
    """Менеджер для работы с BookStack"""
    _instance = None
    _client = None

    def __new__(cls):
        """Реализация паттерна Singleton"""
        if cls._instance is None:
            cls._instance = super(BookStackManager, cls).__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        """Инициализация клиента BookStack"""
        if not hasattr(config, 'BOOKSTACK_URL') or not config.BOOKSTACK_URL:
            logger.warning("BOOKSTACK_URL не задан в конфигурации")
            return

        if not hasattr(config, 'BOOKSTACK_TOKEN_ID') or not config.BOOKSTACK_TOKEN_ID:
            logger.warning("BOOKSTACK_TOKEN_ID не задан в конфигурации")
            return

        if not hasattr(config, 'BOOKSTACK_TOKEN_SECRET') or not config.BOOKSTACK_TOKEN_SECRET:
            logger.warning("BOOKSTACK_TOKEN_SECRET не задан в конфигурации")
            return

        try:
            self._client = BookStackClient(
                base_url=config.BOOKSTACK_URL,
                token_id=config.BOOKSTACK_TOKEN_ID,
                token_secret=config.BOOKSTACK_TOKEN_SECRET
            )
            logger.info(f"BookStack клиент инициализирован для {config.BOOKSTACK_URL}")
        except Exception as e:
            logger.error(f"Ошибка инициализации BookStack клиента: {e}")

    @property
    def client(self) -> Optional[BookStackClient]:
        """Получение клиента BookStack"""
        if not self._client:
            self._initialize()
        return self._client

    def is_available(self) -> bool:
        """Проверка доступности BookStack"""
        return self._client is not None

    def get_book_slug(self, project) -> str:
        """
        Получение слага книги из поля docFolder проекта

        Args:
            project: Объект проекта из БД

        Returns:
            Слаг книги
        """
        # Проверяем наличие docsFolder
        if project.docsFolder and project.docsFolder.strip():
            return project.docsFolder.strip()

        # Формируем стандартный слаг на основе project ID и языка
        return f"jetup-{project.lang}"

    def get_document_html(self, project, doc_slug: str) -> Optional[str]:
        """
        Получение HTML документа проекта

        Args:
            project: Объект проекта из БД
            doc_slug: Слаг документа

        Returns:
            HTML документа или None если документ не найден
        """
        # Формируем ключ кеша
        cache_key = f"{project.projectID}_{project.lang}_{doc_slug}"

        # Проверяем кеш
        cached_html = TemplateCache.get(cache_key)
        if cached_html:
            return cached_html

        # Получаем слаг книги из проекта
        book_slug = self.get_book_slug(project)
        if not book_slug:
            logger.error(f"Не удалось определить слаг книги для проекта {project.projectID} на языке {project.lang}")
            return None

        # Пробуем получить HTML с публичной страницы напрямую
        try:
            url = f"{config.BOOKSTACK_URL}/books/{book_slug}/page/{doc_slug}"
            logger.info(f"Fetching document from public URL: {url}")
            response = requests.get(url)
            response.raise_for_status()

            # Парсим HTML и извлекаем контент
            soup = BeautifulSoup(response.text, 'html.parser')
            # Находим основной контейнер с содержимым
            content_div = soup.select_one('.page-content')

            if content_div:
                first_h1 = content_div.find('h1')
                if first_h1:
                    first_h1.decompose()

                html = str(content_div)
                TemplateCache.set(cache_key, html)
                return html

            logger.warning(f"Content block not found in document at {url}")
            return None

        except Exception as e:
            logger.error(f"Ошибка получения документа {doc_slug} для проекта {project.projectID} напрямую: {e}")

            # Если не удалось получить напрямую, пробуем через API (как запасной вариант)
            if self.is_available():
                try:
                    # Получаем страницу через API
                    page = self.client.get_page_by_slug(book_slug, doc_slug)
                    html = page.get('html')
                    if html:
                        # Сохраняем в кеш
                        TemplateCache.set(cache_key, html)
                        return html
                except BookStackAPIError:
                    logger.warning(
                        f"Документ {doc_slug} не найден для проекта {project.projectID} в книге {book_slug} через API")
                except Exception as e2:
                    logger.error(f"Ошибка получения документа через API: {e2}")

            return None

    def render_template(self, html: str, context: Dict[str, Any]) -> str:
        """
        Рендеринг HTML-шаблона с контекстом

        Args:
            html: HTML-шаблон
            context: Контекст с данными

        Returns:
            Отрендеренный HTML
        """
        try:
            # Создаем окружение Jinja2
            env = jinja2.Environment(
                loader=jinja2.BaseLoader(),
                autoescape=True,
                undefined=jinja2.make_logging_undefined(
                    logger=logger,
                    base=jinja2.DebugUndefined
                )
            )
            template = env.from_string(html)
            return template.render(**context)
        except Exception as e:
            logger.error(f"Ошибка рендеринга шаблона: {e}")
            return html


    def generate_pdf(self, html: str) -> Optional[bytes]:
        """
        Генерация PDF из HTML с использованием pdfkit и wkhtmltopdf

        Args:
            html: HTML-контент

        Returns:
            PDF-документ в виде байтов или None в случае ошибки
        """
        try:
            # Проверяем, что HTML не пустой
            if not html or not html.strip():
                logger.error("Пустой HTML контент для генерации PDF")
                return None

            logger.debug(f"Trying to generate PDF from HTML (length: {len(html)})")

            # Пробуем использовать pdfkit
            try:
                import pdfkit

                # Явно указываем путь к wkhtmltopdf
                wkhtmltopdf_path = '/usr/bin/wkhtmltopdf'
                if not os.path.exists(wkhtmltopdf_path):
                    # Пробуем найти путь с помощью команды which
                    import subprocess
                    try:
                        wkhtmltopdf_path = subprocess.check_output(['which', 'wkhtmltopdf']).decode().strip()
                    except (subprocess.SubprocessError, FileNotFoundError):
                        logger.warning("Could not find wkhtmltopdf using 'which' command")
                        # Поиск в других стандартных местах
                        for path in ['/usr/local/bin/wkhtmltopdf', '/bin/wkhtmltopdf']:
                            if os.path.exists(path):
                                wkhtmltopdf_path = path
                                break

                # Выводим информацию о найденном пути
                logger.info(f"Using wkhtmltopdf from: {wkhtmltopdf_path}")

                # Создаем конфигурацию с явным указанием пути
                config = pdfkit.configuration(wkhtmltopdf=wkhtmltopdf_path)

                # Настройки для wkhtmltopdf
                options = {
                    'encoding': 'UTF-8',
                    'page-size': 'A4',
                    'margin-top': '2cm',
                    'margin-right': '2cm',
                    'margin-bottom': '2cm',
                    'margin-left': '2cm',
                    'footer-right': '[page]/[topage]',
                    'footer-font-size': '9',
                    'no-outline': None,
                    'quiet': ''
                }

                # Добавляем базовые стили для HTML
                styled_html = f"""
                <!DOCTYPE html>
                <html>
                <head>
                    <meta charset="UTF-8">
                    <style>
                        body {{ font-family: Arial, sans-serif; }}
                        h1, h2, h3 {{ color: #333; }}
                        table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
                        th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
                        th {{ background-color: #f2f2f2; }}
                    </style>
                </head>
                <body>
                    {html}
                </body>
                </html>
                """

                # Генерация PDF напрямую в байты с явным указанием конфигурации
                pdf_bytes = pdfkit.from_string(styled_html, False, options=options, configuration=config)

                if pdf_bytes:
                    logger.info(f"PDF successfully generated with pdfkit, size: {len(pdf_bytes)} bytes")
                else:
                    logger.error("pdfkit вернул пустой PDF")

                return pdf_bytes

            except ImportError as e:
                logger.warning(f"pdfkit не установлен или wkhtmltopdf не найден: {e}")
                logger.warning("Пробуем использовать WeasyPrint как запасной вариант")

                # Пробуем использовать WeasyPrint как запасной вариант
                import weasyprint

                # Добавляем базовые стили для HTML
                styled_html = f"""
                <!DOCTYPE html>
                <html>
                <head>
                    <meta charset="UTF-8">
                    <style>
                        body {{ font-family: Arial, sans-serif; margin: 2cm; }}
                        h1, h2, h3 {{ color: #333; }}
                        table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
                        th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
                        th {{ background-color: #f2f2f2; }}
                    </style>
                </head>
                <body>
                    {html}
                </body>
                </html>
                """

                # Генерация PDF
                html_obj = weasyprint.HTML(string=styled_html)
                pdf_bytes = html_obj.write_pdf()

                if pdf_bytes:
                    logger.info(f"PDF successfully generated with WeasyPrint, size: {len(pdf_bytes)} bytes")
                else:
                    logger.error("WeasyPrint вернул пустой PDF")

                return pdf_bytes

        except Exception as e:
            logger.error(f"Ошибка генерации PDF: {e}", exc_info=True)
            return None

    def get_document_url(self, project, doc_slug: str) -> str:
        """
        Получение URL документа

        Args:
            project: Объект проекта из БД
            doc_slug: Слаг документа

        Returns:
            URL документа
        """
        book_slug = self.get_book_slug(project)
        if not book_slug:
            return None
        return f"{config.BOOKSTACK_URL}/books/{book_slug}/page/{doc_slug}"


# Функции для использования в main.py

def get_document_html(project, doc_type: str) -> Optional[str]:
    """
    Получение HTML документа

    Args:
        project: Объект проекта из БД
        doc_type: Тип документа (ключ из PROJECT_DOCUMENTS)

    Returns:
        HTML документа или None если документ не найден
    """
    if not hasattr(config, 'PROJECT_DOCUMENTS'):
        logger.error("PROJECT_DOCUMENTS не определен в config.py")
        return None

    doc_slug = config.PROJECT_DOCUMENTS.get(doc_type)
    if not doc_slug:
        logger.error(f"Неизвестный тип документа: {doc_type}")
        return None

    manager = BookStackManager()

    html = manager.get_document_html(project, doc_slug)
    if html:
        return html
    return None


def render_document(html: str, context: Dict[str, Any]) -> str:
    """
    Рендеринг документа с контекстом

    Args:
        html: HTML-шаблон
        context: Контекст с данными

    Returns:
        Отрендеренный HTML
    """
    manager = BookStackManager()
    return manager.render_template(html, context)


def get_document_as_pdf(html: str) -> Optional[bytes]:
    """
    Преобразование HTML в PDF

    Args:
        html: HTML-контент

    Returns:
        PDF-документ в виде байтов или None в случае ошибки
    """
    manager = BookStackManager()
    return manager.generate_pdf(html)


def get_document_as_temp_file(html: str) -> Optional[tempfile.SpooledTemporaryFile]:
    """
    Преобразование HTML в временный файл PDF

    Args:
        html: HTML-контент

    Returns:
        Временный файл с PDF или None в случае ошибки
    """
    logger.debug("Attempting to convert HTML to PDF temp file")

    if not html:
        logger.error("Получен пустой HTML для преобразования в PDF")
        return None

    # Проверяем содержимое HTML для отладки
    content_preview = html[:500] + ('...' if len(html) > 500 else '')
    logger.debug(f"HTML content preview: {content_preview}")

    pdf_bytes = get_document_as_pdf(html)
    if not pdf_bytes:
        logger.error("Не удалось получить PDF байты")
        return None

    logger.debug(f"PDF bytes received, size: {len(pdf_bytes)} bytes")

    try:
        # Создаем временный файл
        temp_file = tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024, mode='w+b')
        temp_file.write(pdf_bytes)
        temp_file.seek(0)

        # Проверяем размер файла
        current_pos = temp_file.tell()
        temp_file.seek(0, 2)  # Перемещаем курсор в конец файла
        file_size = temp_file.tell()
        temp_file.seek(current_pos)  # Возвращаем курсор в исходное положение

        logger.debug(f"Temporary PDF file created, size: {file_size} bytes")

        return temp_file
    except Exception as e:
        logger.error(f"Ошибка при создании временного файла: {e}", exc_info=True)
        return None


def clear_template_cache():
    """Очистка кеша шаблонов"""
    TemplateCache.clear()


def get_document_url(project, doc_type: str) -> Optional[str]:
    """
    Получение URL документа

    Args:
        project: Объект проекта из БД
        doc_type: Тип документа (ключ из PROJECT_DOCUMENTS)

    Returns:
        URL документа или None если неизвестный тип документа или книга не найдена
    """
    if not hasattr(config, 'PROJECT_DOCUMENTS'):
        logger.error("PROJECT_DOCUMENTS не определен в config.py")
        return None

    doc_slug = config.PROJECT_DOCUMENTS.get(doc_type)
    if not doc_slug:
        logger.error(f"Неизвестный тип документа: {doc_type}")
        return None

    manager = BookStackManager()
    return manager.get_document_url(project, doc_slug)