## @file config.py
# @brief Модуль конфигурации и системы логирования.
# @details Содержит глобальные настройки, пути по умолчанию и механизм сессионного логирования.

import os
import datetime
import json

## Путь к домашней директории пользователя.
HOME_DIR = os.path.expanduser("~")

## Настройки путей по умолчанию.
CONFIG_DEFAULTS = {
    "BROWSE_ROOT_INPUTS": os.path.join(HOME_DIR, "Videos"),
    "BROWSE_ROOT_OUTPUT": os.path.join(HOME_DIR, "Videos", "Edited"),
    "INTRO_DIR": os.path.realpath(os.path.join(HOME_DIR, "Documents", "ffmpeg_video_editor")),
    "SERVER_PORT": 8766
}

## @var SESSION_START_TIME
# @brief Метка времени начала сессии.
# @details Используется для создания уникальных имен лог-файлов, чтобы избежать конфликтов при перезапуске.
SESSION_START_TIME = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

## Основной лог-файл приложения.
APP_LOG_FILE = f"debug_{SESSION_START_TIME}.log"
## Лог-файл для вывода FFmpeg (stdout/stderr).
FFMPEG_LOG_FILE = f"ffmpeg_{SESSION_START_TIME}.log"

# Загрузка пользовательского конфига из JSON
config_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'config.json')
config = CONFIG_DEFAULTS.copy()
if os.path.exists(config_path):
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config.update(json.load(f))
    except Exception: pass

BROWSE_ROOT_INPUTS = os.path.realpath(os.path.expanduser(config["BROWSE_ROOT_INPUTS"]))
BROWSE_ROOT_OUTPUT = os.path.realpath(os.path.expanduser(config["BROWSE_ROOT_OUTPUT"]))
DEFAULT_INTRO_DIR = os.path.realpath(os.path.expanduser(config["INTRO_DIR"]))
SERVER_PORT = int(config["SERVER_PORT"])

os.makedirs(BROWSE_ROOT_INPUTS, exist_ok=True)
os.makedirs(BROWSE_ROOT_OUTPUT, exist_ok=True)

# --- Константы обработки видео ---

## Длительность эффектов Fade In/Out (сек).
FADE_DURATION = 1.0
INTRO_BASE_NAME = "intro_new_sponsored"
VIDEO_ENCODER = "libx264"
FINAL_AUDIO_CODEC = "pcm_s16le"

## Ширина видео для превью (низкое разрешение для скорости).
PREVIEW_WIDTH = 640
## Высота видео для превью.
PREVIEW_HEIGHT = 360
## FPS для превью (для скорости).
PREVIEW_FPS = 20

## @var FRAGMENT_DURATION
# @brief Длительность одного фрагмента предпросмотра (сек).
# @par Rationale (Обоснование)
# Выбрано 10 секунд как компромисс между частотой запросов к серверу и скоростью
# генерации одного фрагмента в режиме перекодирования. Слишком длинные фрагменты
# увеличивают задержку при перемотке (seek).
FRAGMENT_DURATION = 10.0

## @var SEEK_BUFFER
# @brief Запас времени для pre-seek (сек).
# @details FFmpeg начинает чтение файла на 3 секунды раньше нужной точки, чтобы
# корректно декодировать GOP структуру перед точкой разреза.
SEEK_BUFFER = 3.0

## @brief Логирует сообщение в файл и консоль.
# @param msg Текст сообщения.
# @param to_console Дублировать ли вывод в stdout.
def log_debug(msg: str, to_console: bool = True):
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    full_msg = f"[{ts}] {msg}"
    if to_console: print(full_msg)
    with open(APP_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(full_msg + "\n")