import re
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import gspread
from config import GOOGLE_CREDENTIALS_JSON, SCOPES


def get_google_services():
    """
    Возвращает сервисы для работы с Google Sheets и Google Drive API.
    """
    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_JSON, scopes=SCOPES)
    sheets_client = gspread.authorize(creds)
    drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return sheets_client, drive_service


def extract_file_id(url):
    """
    Извлекает идентификатор файла из URL Google Docs.
    """
    match = re.search(r"/d/([a-zA-Z0-9-_]+)", url)
    if match:
        return match.group(1)
    raise ValueError(f"Невозможно извлечь идентификатор файла из URL: {url}")


def download_google_docs_as_html(file_url: str, local_path: str, drive_service) -> None:
    """
    Скачивает документ Google Docs в формате HTML.

    Args:
        file_url: Полный URL Google Docs
        local_path: Локальный путь для сохранения файла
        drive_service: Подключение к Google Drive API
    """
    try:
        # Извлекаем fileId из URL
        file_id = extract_file_id(file_url)

        # Запрашиваем экспорт документа как HTML
        request = drive_service.files().export_media(
            fileId=file_id,
            mimeType='text/html'
        )

        # Сохраняем документ локально
        with open(local_path, "wb") as file:
            downloader = MediaIoBaseDownload(file, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
                print(f"Скачивание {file_id}: {int(status.progress() * 100)}% завершено.")

        # Обрабатываем HTML файл после скачивания
        with open(local_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Конвертируем в шаблон Jinja2
        content = content.replace('{{', '{{ ')  # Добавляем пробелы для лучшей читаемости
        content = content.replace('}}', ' }}')

        # Добавляем базовые стили для PDF
        pdf_styles = '''
        <style>
            @page {
                margin: 2.5cm;
                @top-center {
                    content: "Договор на покупку акций";
                }
                @bottom-center {
                    content: counter(page);
                }
            }
            body {
                font-family: Arial, sans-serif;
                font-size: 12pt;
                line-height: 1.5;
            }
            table {
                width: 100%;
                border-collapse: collapse;
                margin: 1em 0;
            }
            td, th {
                border: 1px solid black;
                padding: 8px;
            }
        </style>
        '''

        # Вставляем стили перед закрывающим тегом </head>
        content = content.replace('</head>', f'{pdf_styles}</head>')

        # Сохраняем обработанный шаблон
        with open(local_path, 'w', encoding='utf-8') as f:
            f.write(content)

    except ValueError as e:
        print(f"Ошибка: {e}")
    except Exception as e:
        print(f"Ошибка при скачивании {file_url}: {e}")
