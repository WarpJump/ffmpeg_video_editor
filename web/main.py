## @file main.py
# @brief Точка входа в приложение.

import asyncio
import http
import json
import os
import re
import uuid
import webbrowser
try:
    from websockets.server import serve
except ImportError:
    from websockets import serve
from urllib.parse import urlparse, parse_qs

from config import *
from timeline import TimelineBuilder
from renderer import FinalRenderer, render_preview_chunk, send_log

async def handle_video_request(path, request_headers):
    query = parse_qs(urlparse(path).query); abs_path = query.get('path', [None])[0]
    if not abs_path or not os.path.exists(abs_path): return (http.HTTPStatus.NOT_FOUND, [], b"Not found")
    file_size = os.path.getsize(abs_path); range_header = request_headers.get("Range")
    headers = {"Content-Type": "video/x-matroska", "Accept-Ranges": "bytes"}
    if range_header:
        m = re.search(r'bytes=(\d*)-(\d*)', range_header)
        start = int(m.group(1)) if m.group(1) else 0; end = int(m.group(2)) if m.group(2) else file_size - 1
        headers.update({"Content-Range": f"bytes {start}-{end}/{file_size}", "Content-Length": str(end - start + 1)})
        with open(abs_path, "rb") as f: f.seek(start); return (http.HTTPStatus.PARTIAL_CONTENT, headers, f.read(end - start + 1))
    headers["Content-Length"] = str(file_size); return (http.HTTPStatus.OK, headers, open(abs_path, "rb").read())

async def websocket_handler(websocket):
    log_debug("Клиент подключился")
    
    # --- ОТПРАВКА НАЧАЛЬНОЙ КОНФИГУРАЦИИ ---
    # Ищем дефолтное интро для отображения в UI
    default_intro = os.path.join(DEFAULT_INTRO_DIR, f"{INTRO_BASE_NAME}_2k.mkv")
    if not os.path.exists(default_intro):
        # Попробуем Home dir
        default_intro = os.path.join(HOME_DIR, f"{INTRO_BASE_NAME}_2k.mkv")
        
    init_config = {
        "action": "config_info",
        "default_intro": default_intro if os.path.exists(default_intro) else "",
        "default_output": BROWSE_ROOT_OUTPUT
    }
    await websocket.send(json.dumps(init_config))
    # ---------------------------------------

    timeline_cache = None
    try:
        async for message in websocket:
            data = json.loads(message); action, params = data.get("action"), data.get("params", {})
            
            if action == "generate_preview_map":
                log_debug("[WS] Запрос карты превью")
                timeline_cache = await TimelineBuilder(params, "/dev/shm" if os.path.exists("/dev/shm") else ".").build()
                total = sum(c.duration for c in timeline_cache)
                clips_data = [c.to_dict() for c in timeline_cache]
                await websocket.send(json.dumps({ 
                    "action": "preview_map_ready", 
                    "total_duration": total,
                    "clips": clips_data,
                    "fragment_duration": FRAGMENT_DURATION
                }))
                
            elif action == "generate_preview_fragment":
                if not timeline_cache: timeline_cache = await TimelineBuilder(params, "/dev/shm" if os.path.exists("/dev/shm") else ".").build()
                req_time = float(data.get("start_time", 0.0))
                total = sum(c.duration for c in timeline_cache)
                if req_time >= total: req_time = max(0, total - 1.0)
                
                path, actual_start = await render_preview_chunk(websocket, timeline_cache, req_time)
                
                if path: 
                    await websocket.send(json.dumps({
                        "action": "preview_fragment_ready", 
                        "start_time": actual_start, 
                        "requested_time": req_time,
                        "relative_path": path
                    }))
            
            elif action == "process":
                # Для process нам нужен Video1 для имени файла.
                # В новом формате берем первый сегмент из списка.
                segments = params.get('segments_list', [])
                video1_path = segments[0].get('video') if segments else params.get('video1')
                
                if not video1_path:
                    await send_log(websocket, "Ошибка: Нет видео для обработки!", msg_type="log")
                    continue

                timeline = await TimelineBuilder(params, "/dev/shm" if os.path.exists("/dev/shm") else ".").build()
                out_dir, base = (params.get('output_dir') or BROWSE_ROOT_OUTPUT), os.path.splitext(os.path.basename(video1_path))[0]
                out_path = os.path.join(out_dir, f"{base}_edited_{str(uuid.uuid4())[:4]}.mkv")
                
                await FinalRenderer(websocket, timeline, out_path).render()
                await send_log(websocket, f"\nГОТОВО! Финальный файл сохранен по пути:", msg_type="header")
                await send_log(websocket, out_path, msg_type="log") 
                await websocket.send(json.dumps({"action": "finished"}))
                
            elif action == "browse_path":
                root = BROWSE_ROOT_OUTPUT if data.get("id") == 'output_dir' else BROWSE_ROOT_INPUTS
                path = os.path.realpath(os.path.join(root, data.get("path", "/").lstrip("/")))
                entries = [{"name": e.name, "type": "dir" if e.is_dir() else "file"} for e in os.scandir(path) if not e.name.startswith('.')]
                entries.sort(key=lambda e: (e['type'] != 'dir', e['name'].lower()))
                await websocket.send(json.dumps({"action": "browse_result", "path": "/" + os.path.relpath(path, root), "entries": entries}))
            
            elif action == "resolve_path":
                root = BROWSE_ROOT_OUTPUT if data.get("id") == 'output_dir' else BROWSE_ROOT_INPUTS
                path = os.path.realpath(os.path.join(root, data.get("path", "/").lstrip("/")))
                await websocket.send(json.dumps({"action": "path_resolved", "full_path": path}))

    except Exception as e:
        import traceback; err = traceback.format_exc(); log_debug(f"CRITICAL: {err}")
        await send_log(websocket, f"Критическая ошибка: {e}")

async def http_server_handler(path, request_headers):
    if "websocket" in request_headers.get("Upgrade", "").lower(): return None
    script_dir = os.path.dirname(os.path.realpath(__file__))
    if path.startswith('/video'): return await handle_video_request(path, request_headers)
    elif path == '/' or path == '/index.html':
        content = open(os.path.join(script_dir, "index.html")).read().replace("%%SERVER_PORT%%", str(SERVER_PORT))
        return (http.HTTPStatus.OK, {"Content-Type": "text/html"}, content.encode())
    elif path == '/style.css': return (http.HTTPStatus.OK, {"Content-Type": "text/css"}, open(os.path.join(script_dir, "style.css"), "rb").read())
    return (http.HTTPStatus.NOT_FOUND, [], b"Not Found")

async def main():
    log_debug(f"Сервер запущен. Порт {SERVER_PORT}. Лог: {APP_LOG_FILE}")
    async with serve(websocket_handler, "0.0.0.0", SERVER_PORT, process_request=http_server_handler):
        print(f"Server: http://127.0.0.1:{SERVER_PORT}"); webbrowser.open_new_tab(f"http://127.0.0.1:{SERVER_PORT}"); await asyncio.Future()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass