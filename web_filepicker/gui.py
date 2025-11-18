import asyncio
import json
import os
import http
import subprocess
import time
import uuid
from typing import Any, Dict, Optional, Tuple
import webbrowser
import websockets
from websockets.server import serve
from urllib.parse import urlparse, parse_qs
import re

# --- КОНФИГУРАЦИЯ ---
FADE_DURATION = 1.0
DEFAULT_INTRO_DIR = os.path.realpath(os.path.join(os.path.expanduser("~"), "Documents", "ffmpeg_video_editor"))
INTRO_BASE_NAME = "intro_new_sponsored"
VIDEO_ENCODER = "libx264"
FINAL_AUDIO_CODEC = "pcm_s16le"
SERVER_PORT = 8765
FORCE_HTTP_STREAMING = True

# Пути (оставьте ваши, если они отличаются, но здесь взяты из ваших логов)
BROWSE_ROOT_INPUTS = os.path.realpath(os.path.expanduser("/run/media/da/FCA4BD89A4BD46C4/lectory/метопты"))
BROWSE_ROOT_OUTPUT = os.path.realpath(os.path.expanduser("/run/media/da/FCA4BD89A4BD46C4/lectory/upload"))
HOME_DIR = os.path.expanduser("~")

print(f"Корень для выбора исходников: {BROWSE_ROOT_INPUTS}")
print(f"Корень для выбора папки вывода: {BROWSE_ROOT_OUTPUT}")

FRAGMENT_DURATION = 10.0

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def get_root_for_context(context_id: str) -> str:
    if context_id == 'output_dir': return BROWSE_ROOT_OUTPUT
    return BROWSE_ROOT_INPUTS

async def secure_path(req_path, root_path):
    if req_path.startswith('/dev/shm'):
        abs_path = os.path.realpath(req_path);
        if abs_path.startswith('/dev/shm') and os.path.exists(abs_path): return abs_path
        else: return None
    clean_path = req_path.lstrip('/\\'); abs_path = os.path.realpath(os.path.join(root_path, clean_path))
    if not abs_path.startswith(root_path): return None
    return abs_path

def hms_to_seconds(time_str):
    if not time_str: return 0
    parts = str(time_str).split(':'); s = 0.0
    try:
        if len(parts) == 3: s = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2: s = int(parts[0]) * 60 + float(parts[1])
        elif len(parts) == 1 and time_str: s = float(time_str)
    except (ValueError, TypeError): s = 0.0
    return s

def seconds_to_hms(seconds):
    h = int(seconds // 3600); m = int((seconds % 3600) // 60); s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:05.2f}"

async def send_log(websocket, message):
    try: await websocket.send(json.dumps({"action": "log", "message": message}))
    except websockets.exceptions.ConnectionClosed: pass

async def run_async_command(websocket, command, title=""):
    if title: await send_log(websocket, f"--- {title} ---")
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    buffer = ""
    while True:
        chunk = await process.stdout.read(256)
        if not chunk: break
        decoded_chunk = buffer + chunk.decode('utf-8', errors='ignore'); lines = decoded_chunk.split('\r'); buffer = lines.pop()
        for line in lines:
            if line.strip(): await send_log(websocket, line.strip())
    if buffer.strip(): await send_log(websocket, buffer.strip())
    await process.wait()
    if process.returncode != 0:
        await send_log(websocket, f"ОШИБКА: Команда завершилась с кодом {process.returncode}")
        raise subprocess.CalledProcessError(process.returncode, " ".join(map(str, command)))

async def get_video_duration(file_path):
    if not os.path.exists(file_path): return 0.0
    try:
        command = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', file_path]
        process = await asyncio.create_subprocess_exec(*command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, _ = await process.communicate()
        return float(stdout.decode().strip()) if process.returncode == 0 and stdout else 0.0
    except Exception: return 0.0

async def get_video_resolution(file_path) -> Optional[Tuple[int, int]]:
    if not os.path.exists(file_path): return None
    try:
        command = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=s=x:p=0', file_path]
        process = await asyncio.create_subprocess_exec(*command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, _ = await process.communicate()
        if process.returncode == 0 and stdout:
            w, h = map(int, stdout.decode().strip().split('x'))
            return w, h
        return None
    except Exception: return None

# --- ЛОГИКА ПРЕДПРОСМОТРА (ИСПРАВЛЕНА) ---

async def handle_preview_fragment(websocket, params, request_start_time: float):
    use_ram = params.get('use_ram', True) and os.path.exists('/dev/shm')
    tmp_dir = "/dev/shm" if use_ram else '.'
    fragment_path = os.path.join(tmp_dir, f"preview_{uuid.uuid4()}.mkv")

    try:
        timeline_map, total_duration = await build_timeline_map(websocket, params)
        if not timeline_map:
            await send_log(websocket, "Не удалось построить карту видео. Проверьте выбор файлов.")
            return

        # Определяем границы фрагмента
        frag_start = request_start_time
        frag_end = min(request_start_time + FRAGMENT_DURATION, total_duration)
        
        # Находим сегменты, которые попадают в этот промежуток времени
        parts_in_fragment = [p for p in timeline_map if p['timeline_start'] < frag_end and p['timeline_start'] + p['duration'] > frag_start]
        if not parts_in_fragment: return
        
        # Определяем целевое разрешение (по первому "segmentX", т.е. основному видео)
        main_video_part = next((p for p in timeline_map if p['id'].startswith('segment')), None)
        if not main_video_part:
            await send_log(websocket, "Не найден основной видео-сегмент для определения разрешения.")
            return
        target_res = await get_video_resolution(main_video_part['source_file'])
        if not target_res:
             await send_log(websocket, f"Не удалось определить разрешение для {main_video_part['source_file']}")
             return

        filters, video_pads, audio_pads = [], [], []
        ffmpeg_inputs = []

        # --- ГЛАВНОЕ ИСПРАВЛЕНИЕ: Используем -ss перед -i для каждого сегмента ---
        # Вместо того чтобы создавать список уникальных файлов, мы добавляем вход 
        # для каждого сегмента отдельно с предварительной перемоткой.
        # Это позволяет мгновенно получать доступ к 50-й минуте видео.

        for i, part in enumerate(parts_in_fragment):
            # Вычисляем, какой кусок исходного файла нам нужен
            t_start_in_part = max(0, frag_start - part['timeline_start'])
            t_end_in_part = min(part['duration'], frag_end - part['timeline_start'])
            
            absolute_start_source = part['source_start_time'] + t_start_in_part
            duration_needed = t_end_in_part - t_start_in_part

            if duration_needed <= 0.01: continue
            
            # ОПТИМИЗАЦИЯ ПЕРЕМОТКИ:
            # Мы прыгаем к (absolute_start_source - 10 секунд). 
            # Это буфер безопасности, чтобы попасть на Keyframe (ключевой кадр).
            seek_buffer = 10.0
            seek_time = max(0, absolute_start_source - seek_buffer)
            
            # Добавляем файл во входные параметры с перемоткой
            # Индекс этого входа будет равен `i`
            ffmpeg_inputs.extend(['-ss', f"{seek_time:.4f}", '-i', part['source_file']])
            
            # Корректируем trim внутри фильтра.
            # Так как мы перемотали файл на `seek_time`, время внутри фильтра начинается с 0 относительно точки перемотки.
            # Нам нужно обрезать "лишний" буфер безопасности.
            trim_start_relative = absolute_start_source - seek_time
            
            current_v_pad = f"[{i}:v]"
            
            # Масштабирование (если нужно)
            part_res = await get_video_resolution(part['source_file'])
            if part_res and part_res != target_res:
                filters.append(f"{current_v_pad}scale={target_res[0]}:{target_res[1]},setsar=1[v{i}_scaled]")
                current_v_pad = f"[v{i}_scaled]"

            # Фильтры обрезки (trim)
            # trim=start=... берет время относительно НАЧАЛА ВХОДА (а вход у нас уже перемотан)
            filters.extend([
                f"{current_v_pad}trim=start={trim_start_relative:.4f}:duration={duration_needed:.4f},setpts=PTS-STARTPTS[v{i}_trimmed]",
                f"[{i}:a]atrim=start={trim_start_relative:.4f}:duration={duration_needed:.4f},asetpts=PTS-STARTPTS[a{i}_trimmed]"
            ])
            
            vf, af = f"[v{i}_trimmed]null[v{i}_faded]", f"[a{i}_trimmed]anull[a{i}_faded]"

            # Обработка FADE IN/OUT (если это края сегментов)
            if part['id'].startswith('segment'):
                # Если это самое начало видео-сегмента (независимо от того, какой сейчас кадр превью)
                if t_start_in_part < 0.01:
                    vf = f"[v{i}_trimmed]fade=in:st=0:d={FADE_DURATION}:alpha=1[v{i}_faded]"
                    af = f"[a{i}_trimmed]afade=t=in:st=0:d={FADE_DURATION}[a{i}_faded]"
                
                # Если это конец видео-сегмента
                if abs((t_start_in_part + duration_needed) - part['duration']) < 0.1 and duration_needed > FADE_DURATION:
                    # Fade out должен начинаться за 1 сек до конца этого куска
                    fade_out_start = duration_needed - FADE_DURATION
                    vf = f"[v{i}_trimmed]fade=out:st={fade_out_start:.4f}:d={FADE_DURATION}:alpha=1[v{i}_faded]"
                    af = f"[a{i}_trimmed]afade=t=out:st={fade_out_start:.4f}:d={FADE_DURATION}[a{i}_faded]"
            
            filters.extend([vf, af])
            video_pads.append(f"[v{i}_faded]")
            audio_pads.append(f"[a{i}_faded]")
            
        if not video_pads: return

        filters.extend([f"{''.join(video_pads)}concat=n={len(video_pads)}:v=1:a=0[v_out]", f"{''.join(audio_pads)}concat=n={len(audio_pads)}:v=0:a=1[a_out]"])
        filter_complex = ";".join(filters)

        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-stats'] + ffmpeg_inputs + ['-filter_complex', filter_complex, '-map', '[v_out]', '-map', '[a_out]', '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency', '-crf', '30', '-threads', '0', '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', fragment_path, '-y']
        
        print(f"\n--- FFmpeg Preview Command ---\n{' '.join(cmd)}\n----------------------------\n")
        
        # Обновленный лог с диапазоном времени
        log_title = f"Рендер фрагмента ({seconds_to_hms(frag_start)} - {seconds_to_hms(frag_end)})"
        await run_async_command(websocket, cmd, log_title)
        
        if os.path.exists(fragment_path) and os.path.getsize(fragment_path) > 0:
            actual_duration = await get_video_duration(fragment_path)
            await websocket.send(json.dumps({"action": "preview_fragment_ready", "start_time": request_start_time, "duration": actual_duration, "relative_path": fragment_path}))
    except Exception as e:
        import traceback
        await send_log(websocket, f"Ошибка предпросмотра: {str(e)}\n{traceback.format_exc()}")
        if os.path.exists(fragment_path): os.remove(fragment_path)
        await websocket.send(json.dumps({"action": "error", "message": "Ошибка генерации предпросмотра"}))


# --- ПОСТРОЕНИЕ КАРТЫ И ОБРАБОТКА ---

async def build_timeline_map(websocket, params):
    timeline_map, total_duration = [], 0.0
    intro_path_to_use = None

    user_selected_intro = params.get('intro_file')
    if user_selected_intro and os.path.exists(user_selected_intro):
        intro_path_to_use = user_selected_intro
    else:
        intro_resolution = params.get('intro_resolution', '2k')
        default_intro_path = os.path.join(DEFAULT_INTRO_DIR, f"{INTRO_BASE_NAME}_{intro_resolution}.mkv")
        if os.path.exists(default_intro_path):
            intro_path_to_use = default_intro_path

    if intro_path_to_use:
        intro_duration = await get_video_duration(intro_path_to_use)
        if intro_duration > 0:
            timeline_map.append({ "id": "intro", "source_file": intro_path_to_use, "timeline_start": 0.0, "duration": intro_duration, "source_start_time": 0 })
            total_duration += intro_duration

    is_single_segment = params.get('is_single_segment') and params.get('mode') == 'single'
    async def process_segment(seg_num, video_path_key, start_key, end_key):
        nonlocal total_duration
        video_path = params.get(video_path_key)
        if not video_path: return
        start, end = hms_to_seconds(params.get(start_key)), hms_to_seconds(params.get(end_key))
        if end <= start:
            end = await get_video_duration(video_path)
        duration = end - start
        if duration > 0:
            timeline_map.append({ "id": f"segment{seg_num}", "source_file": video_path, "timeline_start": total_duration, "duration": duration, "source_start_time": start })
            total_duration += duration
    await process_segment(1, 'video1', 'start1', 'end1')
    if not is_single_segment:
        video2_path = params.get('video2') or (params['video1'] if params.get('mode') == 'single' else None)
        if video2_path:
            params_copy = params.copy(); params_copy['temp_video2'] = video2_path
            await process_segment(2, 'temp_video2', 'start2', 'end2')
    return timeline_map, total_duration

async def handle_preview_generation(websocket, params):
    try:
        await send_log(websocket, "--- Генерация карты предпросмотра ---")
        timeline_map, total_duration = await build_timeline_map(websocket, params)
        await websocket.send(json.dumps({ "action": "preview_map_ready", "timeline_map": timeline_map, "total_duration": total_duration }))
        await send_log(websocket, "Карта предпросмотра успешно создана.")
    except Exception as e:
        await send_log(websocket, f"ОШИБКА генерации карты предпросмотра: {e}")
        await websocket.send(json.dumps({"action": "error", "message": str(e)}))

# Заглушка для handle_processing (оставил пустой, так как в оригинале она была свернута)

async def handle_processing(websocket, params):
    """Главная функция, управляющая процессом обработки видео."""
    temp_files_to_clean = []
    try:
        intro_resolution = params.get('intro_resolution', '2k')
        
        # Проверяем интро в домашней директории в первую очередь
        target_intro_path = os.path.join(HOME_DIR, f"{INTRO_BASE_NAME}_{intro_resolution}.mkv")
        
        # Если интро не найдено в домашней директории, проверяем стандартное расположение
        if not os.path.exists(target_intro_path):
            target_intro_path = os.path.realpath(os.path.join(DEFAULT_INTRO_DIR, f"{INTRO_BASE_NAME}_{intro_resolution}.mkv"))

        if not os.path.exists(target_intro_path):
            await send_log(websocket, f"Целевой файл интро ({os.path.basename(target_intro_path)}) не найден. Поиск источника...")
            
            source_intro = params.get('intro_file') # Приоритет №1: Явно указанный файл
            if not source_intro:
                await send_log(websocket, "Исходник не выбран, поиск стандартных файлов...")
                # Приоритет №2: Поиск стандартных файлов
                fallback_paths = [
                    os.path.join(DEFAULT_INTRO_DIR, f"{INTRO_BASE_NAME}.mkv"),
                    os.path.join(DEFAULT_INTRO_DIR, f"{INTRO_BASE_NAME}.mp4")
                ]
                source_intro = next((p for p in fallback_paths if os.path.exists(p)), None)

            if not source_intro:
                raise FileNotFoundError("Готовое интро не найдено и не удалось найти исходник для его создания. Пожалуйста, выберите файл интро.")

            await send_log(websocket, f"Используем '{os.path.basename(source_intro)}' для создания интро.")
            scale = "scale=1920:1080" if intro_resolution == 'fullhd' else "scale=2560:1440"
            
            # Создаем интро в домашней директории
            target_intro_path = os.path.join(HOME_DIR, f"{INTRO_BASE_NAME}_{intro_resolution}.mkv")
            await run_async_command(websocket, ['ffmpeg','-hide_banner','-loglevel','error','-i',source_intro,'-vf',scale,'-c:v',VIDEO_ENCODER,'-preset','medium','-c:a','copy',target_intro_path,'-y'], f"Создание интро {intro_resolution}")
        
        intro_path = target_intro_path # Теперь мы гарантированно используем правильный путь

        tmp_dir = "/dev/shm" if params.get('use_ram') and os.path.exists('/dev/shm') else '.'
        await send_log(websocket, "--- Этап 1: Подготовка данных ---")
        
        # Определение сегментов для обработки
        is_single_segment = params.get('is_single_segment') and params.get('mode') == 'single'
        segments = []
        if not params.get('video1'): raise ValueError("Не указан Видеофайл 1.")
        
        segments.append({
            'video_orig': params['video1'], 'audio_orig': params.get('audio1') or params['video1'],
            'start': hms_to_seconds(params['start1']), 'end': hms_to_seconds(params['end1'])
        })

        if not is_single_segment:
            video2_path = params['video1'] if params['mode'] == 'single' else params.get('video2')
            if not video2_path: raise ValueError("Не указан Видеофайл 2 для режима двух файлов.")
            
            audio2_source = params.get('audio2') or video2_path
            if params['mode'] == 'single' and not params.get('audio2'):
                audio2_source = params.get('audio1') or video2_path

            segments.append({
                'video_orig': video2_path, 'audio_orig': audio2_source,
                'start': hms_to_seconds(params['start2']), 'end': hms_to_seconds(params['end2'])
            })

        video_concat_parts = [f"file '{intro_path}'"]
        audio_filter_definitions = []
        audio_concat_inputs = "[1:a]"
        ffmpeg_audio_inputs = ['-i', intro_path]

        # Обработка каждого сегмента
        for i, seg in enumerate(segments):
            await send_log(websocket, f"\n--- Обработка сегмента {i+1} ---")
            
            cache_file = f"{seg['video_orig']}.keyframes.txt"
            if not os.path.exists(cache_file):
                await send_log(websocket, f"Анализ I-кадров для {os.path.basename(seg['video_orig'])}...")
                process = await asyncio.create_subprocess_exec('ffprobe','-v','error','-select_streams','v:0','-show_entries','packet=pts_time,flags','-of','csv=p=0', seg['video_orig'], stdout=subprocess.PIPE)
                keyframes_data, _ = await process.communicate()
                with open(cache_file, 'w') as f: f.write('\n'.join([l.split(',')[0] for l in keyframes_data.decode().strip().split('\n') if ',K' in l]))

            with open(cache_file, 'r') as f: keyframes = [float(t) for t in f.read().strip().split('\n') if t]
            
            seg['start_split'] = next((t for t in keyframes if t > seg['start'] + FADE_DURATION), None)
            seg['end_split'] = [t for t in keyframes if t < seg['end'] - FADE_DURATION][-1] if any(t < seg['end'] - FADE_DURATION for t in keyframes) else None
            if not seg['start_split'] or not seg['end_split']: raise ValueError(f"Не найдены точки для бесшовной склейки в сегменте {i+1}. Возможно, он слишком короткий.")
            
            fade_in_path = os.path.join(tmp_dir, f"part{i+1}_fade_in.mkv"); temp_files_to_clean.append(fade_in_path)
            fade_out_path = os.path.join(tmp_dir, f"part{i+1}_fade_out.mkv"); temp_files_to_clean.append(fade_out_path)
            
            await run_async_command(websocket, ['ffmpeg','-hide_banner','-loglevel', 'error','-stats','-ss', str(seg['start']), '-to', str(seg['start_split']), '-i', seg['video_orig'], '-an', '-vf', f"fade=in:st=0:d={FADE_DURATION},setpts=PTS-STARTPTS", '-c:v', VIDEO_ENCODER, '-preset', 'ultrafast', fade_in_path, '-y'], "Создание fade-in")
            
            fout_rel_start = seg['end'] - seg['end_split'] - FADE_DURATION
            await run_async_command(websocket, ['ffmpeg','-hide_banner','-loglevel', 'error','-stats','-ss', str(seg['end_split']), '-to', str(seg['end']), '-i', seg['video_orig'], '-an', '-vf', f"fade=out:st={fout_rel_start:.4f}:d={FADE_DURATION},setpts=PTS-STARTPTS", '-c:v', VIDEO_ENCODER, '-preset', 'ultrafast', fade_out_path, '-y'], "Создание fade-out")

            video_concat_parts.extend([f"file '{fade_in_path}'", f"file '{seg['video_orig']}'\ninpoint {seg['start_split']}\noutpoint {seg['end_split']}", f"file '{fade_out_path}'"])
            
            ain_idx = 1 + 1 + i
            ffmpeg_audio_inputs.extend(['-i', seg['audio_orig']])
            audio_filter_definitions.extend([
                f"[{ain_idx}:a]asplit=3[aud{i}s1][aud{i}s2][aud{i}s3]",
                f"[aud{i}s1]atrim=start={seg['start']}:end={seg['start_split']},asetpts=PTS-STARTPTS,afade=t=in:st=0:d={FADE_DURATION}[aud{i}fi]",
                f"[aud{i}s2]atrim=start={seg['start_split']}:end={seg['end_split']},asetpts=PTS-STARTPTS[aud{i}mb]",
                f"[aud{i}s3]atrim=start={seg['end_split']}:end={seg['end']},asetpts=PTS-STARTPTS,afade=t=out:st={fout_rel_start:.4f}:d={FADE_DURATION}[aud{i}fo]"
            ])
            audio_concat_inputs += f"[aud{i}fi][aud{i}mb][aud{i}fo]"

        # Финальная сборка
        await send_log(websocket, "\n--- Этап 3: Финальная сборка ---")
        concat_path = os.path.join(tmp_dir, "concat.txt"); temp_files_to_clean.append(concat_path)
        with open(concat_path, 'w', encoding='utf-8') as f: f.write("\n".join(video_concat_parts))

        num_audio_outputs = 1 + (len(segments) * 3)
        final_concat_filter = f"{audio_concat_inputs}concat=n={num_audio_outputs}:v=0:a=1[fa]"
        filter_complex = ";".join(audio_filter_definitions + [final_concat_filter])
        
        base_name, _ = os.path.splitext(os.path.basename(segments[0]['video_orig']))
        output_name = f"{params.get('intro_resolution', '2k')}_{base_name}_final_edit.mkv"

        # Используем выбранную директорию, если она есть, иначе - дефолтную выходную директорию
        output_dir = params.get('output_dir') or "BROWSE_ROOT_OUTPUT"
        if not os.path.isdir(output_dir):
            raise ValueError(f"Выходная директория не существует: {output_dir}")

        output_path = os.path.join(output_dir, output_name)

        final_cmd = ['ffmpeg','-hide_banner','-loglevel','error',  '-stats','-f','concat','-safe','0','-i', concat_path] + ffmpeg_audio_inputs + ['-filter_complex', filter_complex, '-map','0:v','-map','[fa]', '-c:v','copy','-r','60', '-c:a', FINAL_AUDIO_CODEC, '-movflags', '+faststart', output_path,'-y']
        
        await run_async_command(websocket, final_cmd, "Запускаем финальную сборку")
        await send_log(websocket, f"\nУСПЕХ! Финальный файл сохранен: {output_path}")

    except Exception as e:
        import traceback
        await send_log(websocket, f"\n\nКРИТИЧЕСКАЯ ОШИБКА: {str(e)}\n{traceback.format_exc()}\n")
    finally:
        await send_log(websocket, "\n--- Очистка ---")
        for f in temp_files_to_clean:
            if os.path.exists(f): 
                try: os.remove(f); await send_log(websocket, f"Удалено: {f}")
                except OSError as e: await send_log(websocket, f"Не удалось удалить {f}: {e}")
        await websocket.send(json.dumps({"action": "finished"}))

# --- HTTP & WEBSOCKET HANDLERS ---

async def handle_video_request(path, request_headers):
    query = parse_qs(urlparse(path).query)
    rel_path_from_query = query.get('path', [None])[0] or query.get('file', [None])[0]
    if not rel_path_from_query: return (http.HTTPStatus.BAD_REQUEST, [], b"Missing path/file parameter")
    if rel_path_from_query.startswith('/dev/shm'):
        abs_path = os.path.realpath(rel_path_from_query)
        if not (abs_path.startswith('/dev/shm') and os.path.exists(abs_path)): return (http.HTTPStatus.NOT_FOUND, [], b"Temp file not found")
    else:
        abs_path = os.path.realpath(os.path.join(BROWSE_ROOT_INPUTS, rel_path_from_query.replace('/', os.sep)))
        if not (abs_path.startswith(BROWSE_ROOT_INPUTS) and os.path.exists(abs_path)): return (http.HTTPStatus.NOT_FOUND, [], b"File not found or access denied")
    try:
        file_size = os.path.getsize(abs_path)
        range_header = request_headers.get("Range")
        headers = { "Accept-Ranges": "bytes", "Content-Type": "video/mp4" if abs_path.lower().endswith('.mp4') else "video/x-matroska" }
        if range_header:
            start_str, end_str = re.search(r'bytes=(\d*)-(\d*)', range_header).groups()
            start = int(start_str) if start_str else 0; end = int(end_str) if end_str else file_size - 1
            length = end - start + 1
            headers.update({ "Content-Range": f"bytes {start}-{end}/{file_size}", "Content-Length": str(length) })
            with open(abs_path, "rb") as f: f.seek(start); body = f.read(length)
            return (http.HTTPStatus.PARTIAL_CONTENT, headers, body)
        else:
            headers["Content-Length"] = str(file_size)
            return (http.HTTPStatus.OK, headers, open(abs_path, "rb").read())
    except Exception as e:
        print(f"Ошибка при отдаче файла {abs_path}: {e}")
        return (http.HTTPStatus.INTERNAL_SERVER_ERROR, [], b"Server error")

async def websocket_handler(websocket):
    print("Клиент WebSocket подключен.")
    try:
        async for message in websocket:
            data = json.loads(message)
            action, params = data.get("action"), data.get("params", {})
            if action == "generate_preview_map": asyncio.create_task(handle_preview_generation(websocket, params))
            elif action == "generate_preview_fragment": asyncio.create_task(handle_preview_fragment(websocket, params, float(data.get("start_time", 0.0))))
            elif action == "process": asyncio.create_task(handle_processing(websocket, params))
            elif action in ["browse_path", "resolve_path"]:
                context_id, req_path = data.get("id"), data.get("path", "/")
                root_path = get_root_for_context(context_id)
                clean_path = req_path.lstrip('/\\'); abs_path = os.path.realpath(os.path.join(root_path, clean_path))
                if not abs_path.startswith(root_path):
                    await websocket.send(json.dumps({"action": "error", "message": "Доступ запрещен"}))
                    continue
                if action == "browse_path":
                    try:
                        entries = [{"name": e.name, "type": "dir" if e.is_dir() else "file"} for e in os.scandir(abs_path) if not e.name.startswith('.')]
                        entries.sort(key=lambda e: (e['type'] != 'dir', e['name'].lower()))
                        display_path = '/' + os.path.relpath(abs_path, root_path).replace('\\', '/'); display_path = '/' if display_path == '/.' else display_path
                        await websocket.send(json.dumps({"action": "browse_result", "path": display_path, "entries": entries}))
                    except Exception as e: await websocket.send(json.dumps({"action": "error", "message": str(e)}))
                elif action == "resolve_path":
                    await websocket.send(json.dumps({"action": "path_resolved", "full_path": abs_path}))
    except websockets.exceptions.ConnectionClosed: print("Клиент отключился.")

async def http_server_handler(path, request_headers):
    if "Upgrade" in request_headers and request_headers["Upgrade"].lower() == "websocket": return None 
    script_dir = os.path.dirname(os.path.realpath(__file__))
    if path.startswith('/video'): return await handle_video_request(path, request_headers)
    elif path == '/' or path == '/index.html': file_path, content_type = os.path.join(script_dir, "index.html"), "text/html; charset=utf-8"
    elif path == '/style.css': file_path, content_type = os.path.join(script_dir, "style.css"), "text/css; charset=utf-8"
    else: return (http.HTTPStatus.NOT_FOUND, [], b"Not Found")
    try:
        with open(file_path, 'r', encoding='utf-8') as f: content = f.read()
        if path == '/' or path == '/index.html': content = content.replace("%%SERVER_PORT%%", str(SERVER_PORT))
        return (http.HTTPStatus.OK, {"Content-Type": content_type}, content.encode())
    except FileNotFoundError: return (http.HTTPStatus.NOT_FOUND, [], f"File not found: {os.path.basename(file_path)}".encode())

async def main():
    async with serve(websocket_handler, "127.0.0.1", SERVER_PORT, process_request=http_server_handler):
        url = f"http://127.0.0.1:{SERVER_PORT}"; print(f"Сервер запущен. Откройте в браузере: {url}"); webbrowser.open_new_tab(url)
        await asyncio.Future()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: print("\nПриложение остановлено.")
    except OSError as e: print(f"\nОШИБКА: Порт {SERVER_PORT} уже занят." if e.errno == 98 else f"Системная ошибка: {e}"); exit(1)