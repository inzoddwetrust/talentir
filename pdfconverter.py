import os
from pdf2image import convert_from_path
from moviepy.editor import ImageSequenceClip

# Глобальные переменные для настройки
RESOLUTION = (1920, 1080)  # Разрешение 16:9 для изображений и видео
PAGE_DURATION = 10  # Время отображения каждой страницы (в секундах)
FPS = 24  # Количество кадров в секунду
WORKING_DIR = "./temp"  # Рабочая директория для временных файлов


# Функция для конвертации первой страницы PDF в изображение
def convert_first_page_to_image(pdf_path):
    if not os.path.exists(WORKING_DIR):
        os.makedirs(WORKING_DIR)

    # Конвертация первой страницы PDF
    images = convert_from_path(pdf_path, size=RESOLUTION)  # Используем путь к файлу
    first_page_image = images[0]

    # Сохраняем первую страницу как изображение
    image_path = os.path.join(WORKING_DIR, "first_page.png")
    first_page_image.save(image_path)

    return image_path


# Функция для конвертации PDF в видео
def convert_pdf_to_video(pdf_path):
    if not os.path.exists(WORKING_DIR):
        os.makedirs(WORKING_DIR)

    # Конвертация всех страниц PDF в изображения
    images = convert_from_path(pdf_path, size=RESOLUTION)  # Используем путь к файлу
    image_files = []

    # Сохраняем каждую страницу как изображение
    for i, image in enumerate(images):
        image_path = os.path.join(WORKING_DIR, f"page_{i}.png")
        image.save(image_path)
        image_files.append(image_path)

    # Создание видеоролика из изображений
    clip = ImageSequenceClip(image_files, durations=[PAGE_DURATION] * len(image_files))
    clip = clip.set_fps(FPS)

    # Путь для сохранения видео
    video_output_path = os.path.join(WORKING_DIR, "output_video.mp4")
    clip.write_videofile(video_output_path, codec="libx264")

    # Удаление временных файлов изображений
    for image_file in image_files:
        os.remove(image_file)

    return video_output_path
