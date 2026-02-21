## @file hardware.py
# @brief Модуль детекции аппаратного ускорения.

import os
import subprocess
import asyncio
from config import log_debug

# Глобальный кэш конфигурации
_CACHED_CONFIG = None

async def check_encoder(encoder_name: str, test_cmd_args: list) -> bool:
    """Пытается запустить ffmpeg с тестовыми аргументами для проверки энкодера."""
    try:
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', 'color=black:s=128x128:r=1', '-frames:v', '1'] + test_cmd_args + ['-f', 'null', '-']
        print(" ".join(cmd))
        process = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, stderr = await process.communicate()
        if process.returncode == 0:
            return True
        # log_debug(f"Check {encoder_name} failed: {stderr.decode()}")
        return False
    except Exception as e:
        log_debug(f"Check {encoder_name} error: {e}")
        return False

async def get_hardware_config():
    global _CACHED_CONFIG
    if _CACHED_CONFIG:
        return _CACHED_CONFIG

    log_debug("Детекция аппаратного ускорения...")

    # 1. Проверка VAAPI (Intel / AMD) - Приоритет для N100
    # Проверяем наличие устройства рендеринга
    vaapi_device = "/dev/dri/renderD128"
    if os.path.exists(vaapi_device):
        # Тестовая команда: инициализация девайса, upload в GPU, кодирование
        is_working = await check_encoder("h264_vaapi", [
            '-init_hw_device', f'vaapi=va:{vaapi_device}',
            '-filter_hw_device', 'va',
            '-vf', 'format=nv12,hwupload',
            '-c:v', 'h264_vaapi'
        ])
        
        if is_working:
            log_debug(f"✅ Используем VAAPI (Intel/AMD) на {vaapi_device}")
            _CACHED_CONFIG = {
                'type': 'vaapi',
                'global_args': ['-init_hw_device', f'vaapi=va:{vaapi_device}', '-filter_hw_device', 'va'],
                'upload_filter': 'format=nv12,hwupload', # Загрузка из RAM в GPU
                'codec': 'h264_vaapi',
                'codec_args': ['-qp', '28', '-async_depth', '1']
            }
            return _CACHED_CONFIG

    # 2. Проверка NVENC (Nvidia)
    is_working = await check_encoder("h264_nvenc", ['-c:v', 'h264_nvenc'])
    if is_working:
        log_debug("✅ Используем NVENC (Nvidia)")
        _CACHED_CONFIG = {
            'type': 'nvenc',
            'global_args': [],
            'upload_filter': 'format=yuv420p', # NVENC сам заберет, но формат лучше уточнить
            'codec': 'h264_nvenc',
            'codec_args': ['-preset', 'p1', '-tune', 'll', '-delay', '0'] # Low Latency
        }
        return _CACHED_CONFIG

    # 3. Fallback: CPU (libx264)
    log_debug("⚠️ Аппаратное ускорение не найдено. Используем CPU (libx264 zerolatency).")
    _CACHED_CONFIG = {
        'type': 'cpu',
        'global_args': [],
        'upload_filter': '',
        'codec': 'libx264',
        'codec_args': ['-preset', 'ultrafast', '-tune', 'zerolatency', '-crf', '28']
    }
    return _CACHED_CONFIG