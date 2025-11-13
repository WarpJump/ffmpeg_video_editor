import asyncio
import json
import os
import http
import subprocess
import webbrowser
import websockets
from websockets.server import serve
from urllib.parse import urlparse, parse_qs
import re
# ==============================================================================
# ---                        ГЛАВНЫЕ НАСТРОЙКИ                                ---
# ==============================================================================
FADE_DURATION = 1.0
DEFAULT_INTRO_DIR = os.path.realpath("..")
INTRO_BASE_NAME = "intro_new_sponsored"
VIDEO_ENCODER = "libx264"
FINAL_AUDIO_CODEC = "pcm_s16le"
SERVER_PORT = 8765

# --- НАСТРОЙКИ ДЛЯ ФАЙЛОВОГО БРАУЗЕРА ---
BROWSE_ROOT = os.path.realpath(os.path.expanduser("~/Videos"))

# ==============================================================================
# ---                      ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ                           ---
# ==============================================================================

async def secure_path(req_path):
    """Проверяет, что путь безопасен и находится внутри BROWSE_ROOT."""
    clean_path = req_path.lstrip('/\\')
    abs_path = os.path.realpath(os.path.join(BROWSE_ROOT, clean_path))
    if not abs_path.startswith(BROWSE_ROOT):
        print(f"Попытка недопустимого доступа: {req_path}")
        return None
    return abs_path

def hms_to_seconds(time_str):
    """Конвертирует время из формата ЧЧ:ММ:СС в секунды."""
    if not time_str: return 0
    parts = str(time_str).split(':')
    s = 0.0
    try:
        if len(parts) == 3: s = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2: s = int(parts[0]) * 60 + float(parts[1])
        elif len(parts) == 1 and time_str: s = float(time_str)
    except ValueError:
        s = 0.0
    return s

async def send_log(websocket, message):
    """Отправляет сообщение в лог веб-интерфейса."""
    try:
        await websocket.send(json.dumps({"action": "log", "message": message}))
    except websockets.exceptions.ConnectionClosed:
        pass

async def run_async_command(websocket, command, title=""):
    """Асинхронно выполняет команду FFmpeg и стримит её вывод в лог."""
    if title: await send_log(websocket, f"--- {title} ---")
    
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )
    
    buffer = ""
    while True:
        chunk = await process.stdout.read(128)
        if not chunk: break
        
        decoded_chunk = buffer + chunk.decode(errors='ignore')
        lines = decoded_chunk.replace('\r', '\n').split('\n')
        buffer = lines.pop()
        
        for line in lines:
            stripped_line = line.strip()
            if stripped_line: await send_log(websocket, stripped_line)

    if buffer.strip(): await send_log(websocket, buffer.strip())

    await process.wait()
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, " ".join(map(str, command)))

# ==============================================================================
# ---                     ОСНОВНАЯ ЛОГИКА ОБРАБОТКИ                          ---
# ==============================================================================

async def handle_processing(websocket, params):
    """Главная функция, управляющая процессом обработки видео."""
    temp_files_to_clean = []
    try:
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

        # Подготовка интро
        intro_resolution = params.get('intro_resolution', '2k')
        intro_path = os.path.realpath(os.path.join(DEFAULT_INTRO_DIR, f"{INTRO_BASE_NAME}_{intro_resolution}.mkv"))
        if not os.path.exists(intro_path):
            source_intro = params.get('intro_file')
            if not source_intro: raise FileNotFoundError("Готовое интро не найдено, выберите исходник!")
            scale = "scale=2560:1440" if intro_resolution == '2k' else "scale=1920:1080"
            await run_async_command(websocket, ['ffmpeg','-hide_banner','-loglevel', 'error', '-i', source_intro, '-vf', scale, '-c:v', VIDEO_ENCODER, '-preset', 'medium', '-c:a', 'copy', intro_path, '-y'], f"Создание интро {intro_resolution}")

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

        # Используем выбранную директорию, если она есть, иначе - директорию из video1
        output_dir = params.get('output_dir') or os.path.dirname(params['video1'])
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
# ==============================================================================
# ---                        СЕРВЕРНАЯ ЧАСТЬ                                 ---
# ==============================================================================

async def handle_video_request(path, request_headers):
    """Обрабатывает HTTP запросы на видеофайлы с поддержкой Range-запросов."""
    
    # 1. Безопасность: извлекаем путь к файлу из query-параметра ?path=...
    parsed_path = urlparse(path)
    query_params = parse_qs(parsed_path.query)
    file_rel_path = query_params.get('path', [None])[0]

    if not file_rel_path:
        return (http.HTTPStatus.BAD_REQUEST, [], b"Missing 'path' parameter")

    # 2. Проверяем, что путь находится внутри разрешенной директории
    abs_path = await secure_path(file_rel_path)
    if not abs_path or not os.path.isfile(abs_path):
        return (http.HTTPStatus.NOT_FOUND, [], b"File not found or access denied")

    file_size = os.path.getsize(abs_path)
    range_header = request_headers.get('Range')
    
    headers = {
        "Content-Type": "video/mp4", # Можно использовать и video/webm, mkv и т.д.
        "Accept-Ranges": "bytes",
        "Content-Length": str(file_size),
    }

    if range_header:
        # 3. Парсим Range-заголовок, чтобы отдать только часть файла
        range_match = re.match(r'bytes=(\d+)-(\d*)', range_header)
        if not range_match:
            return (http.HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, headers, b"Invalid Range header")
        
        start_byte = int(range_match.group(1))
        end_byte_str = range_match.group(2)
        end_byte = int(end_byte_str) if end_byte_str else file_size - 1
        
        if start_byte >= file_size or end_byte >= file_size:
            return (http.HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, headers, b"Range out of bounds")

        length = end_byte - start_byte + 1
        headers["Content-Length"] = str(length)
        headers["Content-Range"] = f"bytes {start_byte}-{end_byte}/{file_size}"

        with open(abs_path, 'rb') as f:
            f.seek(start_byte)
            data = f.read(length)
        
        return (http.HTTPStatus.PARTIAL_CONTENT, headers, data)
    else:
        # 4. Если Range не указан, можно отдать файл целиком (но браузер обычно сам запросит с Range)
        # Для простоты вернем OK, браузер сам сделает следующий запрос с Range
        return (http.HTTPStatus.OK, headers, b"")


async def websocket_handler(websocket):
    """Обрабатывает сообщения от клиента WebSocket."""
    # ... (код этой функции остается без изменений)
    print("Клиент WebSocket подключен.")
    try:
        async for message in websocket:
            data = json.loads(message)
            action = data.get("action")
            if action in ["browse_path", "resolve_path"]:
                req_path = data.get("path", "/")
                print(f"Получено: {action} для '{req_path}'")
                abs_path = await secure_path(req_path)
                if not abs_path:
                    await websocket.send(json.dumps({"action": "error", "message": "Доступ запрещен"}))
                    continue
                if action == "browse_path":
                    try:
                        entries = [
                            {"name": e.name, "type": "dir" if e.is_dir() else "file"}
                            for e in os.scandir(abs_path) if not e.name.startswith('.')
                        ]
                        entries.sort(key=lambda e: (e['type'] != 'dir', e['name'].lower()))
                        display_path = '/' + os.path.relpath(abs_path, BROWSE_ROOT).replace('\\', '/')
                        if display_path == '/.': display_path = '/'
                        await websocket.send(json.dumps({"action": "browse_result", "path": display_path, "entries": entries}))
                    except Exception as e:
                        print(f"Ошибка чтения '{abs_path}': {e}")
                elif action == "resolve_path":
                    # ВАЖНО: Отправляем относительный путь для безопасности
                    rel_path = os.path.relpath(abs_path, BROWSE_ROOT).replace('\\', '/')
                    await websocket.send(json.dumps({"action": "path_resolved", "full_path": abs_path, "relative_path": rel_path}))

            elif action == "process":
                asyncio.create_task(handle_processing(websocket, data.get("params", {})))
    except websockets.exceptions.ConnectionClosed:
        print("Клиент отключился.")


async def http_server_handler(path, request_headers):
    """Обрабатывает HTTP-запросы, отдавая файлы интерфейса или видео."""
    if "Upgrade" in request_headers and request_headers["Upgrade"].lower() == "websocket":
        return None 
    
    script_dir = os.path.dirname(os.path.realpath(__file__))
    
    # НОВЫЙ МАРШРУТИЗАТОР
    if path.startswith('/video'):
        # Если запрос начинается с /video, передаем его новому обработчику
        # Также нам понадобится модуль re для парсинга Range
        import re 
        return await handle_video_request(path, request_headers)
    elif path == '/' or path == '/index.html':
        file_path = os.path.join(script_dir, "index.html")
        content_type = "text/html; charset=utf-8"
    elif path == '/style.css':
        file_path = os.path.join(script_dir, "style.css")
        content_type = "text/css; charset=utf-8"
    else:
        return (http.HTTPStatus.NOT_FOUND, [], b"Not Found")

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        if path == '/' or path == '/index.html':
            content = content.replace("%%SERVER_PORT%%", str(SERVER_PORT))
            
        return (http.HTTPStatus.OK, {"Content-Type": content_type}, content.encode())
    except FileNotFoundError:
        return (http.HTTPStatus.NOT_FOUND, [], f"File not found: {os.path.basename(file_path)}".encode())


async def main():
    """Главная функция запуска сервера."""
    
    async with serve(websocket_handler, "127.0.0.1", SERVER_PORT, process_request=http_server_handler):
        url = f"http://127.0.0.1:{SERVER_PORT}"
        print(f"Сервер запущен. Откройте в браузере: {url}")
        webbrowser.open_new_tab(url)
        await asyncio.Future()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nПриложение остановлено.")
    except OSError as e:
        if e.errno == 98: print(f"\nОШИБКА: Порт {SERVER_PORT} уже занят.")
        else: print(f"Системная ошибка: {e}")
        exit(1)