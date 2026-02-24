## @file config.py
# @brief Модуль конфигурации и системы логирования.

import os
import datetime
import json

HOME_DIR = os.path.expanduser("~")

CONFIG_DEFAULTS = {
    "BROWSE_ROOT_INPUTS": os.path.join(HOME_DIR, "Videos"),
    "BROWSE_ROOT_OUTPUT": os.path.join(HOME_DIR, "Videos", "Edited"),
    "INTRO_DIR": os.path.realpath(os.path.join(HOME_DIR, "Documents", "ffmpeg_video_editor")),
    "SERVER_PORT": 8766
}

SESSION_START_TIME = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

APP_LOG_FILE = f"debug_{SESSION_START_TIME}.log"
# Разделенные логи FFmpeg
FFMPEG_PREVIEW_LOG = f"ffmpeg_preview_{SESSION_START_TIME}.log"
FFMPEG_FINAL_LOG = f"ffmpeg_final_{SESSION_START_TIME}.log"

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

FADE_DURATION = 1.0
INTRO_BASE_NAME = "intro_new_sponsored"
VIDEO_ENCODER = "libx264"
FINAL_AUDIO_CODEC = "pcm_s16le"

PREVIEW_WIDTH = 640
PREVIEW_HEIGHT = 360
PREVIEW_FPS = 20
FRAGMENT_DURATION = 10.0
SEEK_BUFFER = 3.0

def log_debug(msg: str, to_console: bool = True):
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
    full_msg = f"[{ts}] {msg}"
    if to_console: print(full_msg)
    with open(APP_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(full_msg + "\n")