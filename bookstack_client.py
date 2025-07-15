import logging
import requests
from typing import Dict, Any
from urllib.parse import urljoin

logger = logging.getLogger(__name__)


class BookStackAPIError(Exception):
    """Исключение для ошибок API BookStack"""
    pass


class BookStackClient:
    def __init__(self, base_url: str, token_id: str, token_secret: str):
        """Инициализация клиента API BookStack"""
        self.base_url = base_url.rstrip('/')
        self.token_id = token_id
        self.token_secret = token_secret
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Token {token_id}:{token_secret}',
            'Accept': 'application/json'
        })

    def _make_request(self, endpoint: str) -> Dict[str, Any]:
        """Выполнение GET-запроса к API BookStack"""
        url = urljoin(f"{self.base_url}/api/", endpoint.lstrip('/'))

        try:
            response = self.session.get(url)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            error_msg = f"BookStack API error: {e}"
            logger.error(error_msg)
            raise BookStackAPIError(error_msg)

    def get_page_by_slug(self, book_slug: str, page_slug: str) -> Dict[str, Any]:
        """
        Получение страницы по слагам книги и страницы

        Args:
            book_slug: Слаг книги (например, 'project_42_ru')
            page_slug: Слаг страницы (например, 'option-alienation-agreement')

        Returns:
            Данные страницы
        """
        # Получаем книгу
        book = self._make_request(f'/books/slug/{book_slug}')

        # Получаем страницы книги
        pages = self._make_request(f'/books/{book["id"]}/pages')

        # Ищем нужную страницу
        for page in pages.get('data', []):
            if page['slug'] == page_slug:
                # Получаем полные данные страницы
                return self._make_request(f'/pages/{page["id"]}')

        raise BookStackAPIError(f"Page {page_slug} not found in book {book_slug}")

    def get_public_url(self, book_slug: str, page_slug: str = None) -> str:
        """Формирование публичного URL для книги или страницы"""
        if page_slug:
            return f"{self.base_url}/books/{book_slug}/page/{page_slug}"
        return f"{self.base_url}/books/{book_slug}"