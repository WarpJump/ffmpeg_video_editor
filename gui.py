import asyncio
import json
import os
import http
import subprocess
import webbrowser
from tkinter import Tk, filedialog
import websockets
from websockets.server import serve

# ==============================================================================
# ---                        ГЛАВНЫЕ НАСТРОЙКИ                                ---
# ==============================================================================
FADE_DURATION = 1.0
DEFAULT_INTRO_DIR = os.path.realpath("..")
INTRO_BASE_NAME = "intro_new_sponsored"
VIDEO_ENCODER = "libx264"
FINAL_AUDIO_CODEC = "pcm_s16le"
SERVER_PORT = 8765

HTML_CONTENT = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Видео Редактор v21 (Автономный)</title>
    <style>
        :root { --main-bg: #282a36; --main-fg: #f8f8f2; --panel-bg: #44475a; --border-color: #6272a4; --accent-green: #50fa7b; --accent-purple: #bd93f9; --accent-pink: #ff79c6; --log-bg: #21222c;}
        body { font-family: sans-serif; background: var(--main-bg); color: var(--main-fg); max-width: 1200px; margin: 2em auto; padding: 1em; }
        .container { display: flex; flex-wrap: wrap; gap: 20px; }
        .form-column { flex: 1; min-width: 400px; } 
        .preview-column { flex: 2; min-width: 400px;}
        h1, h2 { color: var(--accent-green); border-bottom: 2px solid var(--panel-bg); }
        input[type="text"] { flex-grow: 1; padding: 8px; background: var(--panel-bg); color: var(--main-fg); border: 1px solid var(--border-color); border-radius: 4px; box-sizing: border-box; }
        button { background-color: var(--border-color); color: var(--main-fg); border: none; padding: 8px; border-radius: 4px; cursor: pointer; transition: background-color 0.2s; width: 100%; margin-bottom: 10px; }
        button:hover { background-color: var(--accent-purple); }
        button.action-btn { background: var(--accent-green); color: var(--main-bg); padding: 15px; font-size: 1.2em; }
        button:disabled { background: var(--panel-bg); cursor: not-allowed; }
        .jump-btn { background: var(--accent-purple); width: 40px; height: 38px; font-size: 1.5em; padding: 0; margin-left: 5px; flex-shrink: 0;}
        video { width: 100%; border: 1px solid var(--panel-bg); background: black; }
        label { color: var(--accent-purple); } legend { color: var(--accent-pink); }
        fieldset { border: 1px solid var(--panel-bg); margin-bottom: 15px; }
        #log-output { background: var(--log-bg); border: 1px solid var(--panel-bg); height: 400px; overflow-y: scroll; white-space: pre-wrap; font-family: monospace; }
        .hidden { display: none; }
        .time-input-group { display: flex; align-items: center; margin-bottom: 10px; }
        .file-path-display { padding: 8px; background: #333; border-radius: 4px; min-height: 1.2em; word-break: break-all; margin-top: 5px; margin-bottom: 10px; }
    </style>
</head>
<body>
    <h1>Видео Редактор v21 (Автономный)</h1>
     <div class="container">
        <div class="form-column">
            <fieldset><legend>1. Настройки</legend>
                <label>Режим Работы:</label>
                <div>
                    <input type="radio" id="singleFile" name="mode" value="single" checked onchange="toggleMode()"> 
                    <label for="singleFile" title="Обработать два сегмента из одного и того же видео/аудио файла.">Один файл</label>
                    <input type="radio" id="twoFiles" name="mode" value="two" onchange="toggleMode()"> 
                    <label for="twoFiles" title="Обработать два сегмента из двух разных видео/аудио файлов.">Два файла</label>
                </div>
                <div id="single_segment_toggle_div" style="margin-top:10px;">
                    <input type="checkbox" id="is_single_segment" name="is_single_segment" onchange="toggleMode()"> 
                    <label for="is_single_segment" title="Вырезать один непрерывный кусок из видео, добавив интро и плавные переходы в начале и конце.">Обработать как единый ролик</label>
                </div>
                <div style="margin-top:15px;">
                    <input type="checkbox" id="use_ram" checked> 
                    <label for="use_ram" title="Временные файлы будут созданы в /dev/shm (ОЗУ). Работает только на Linux. Ускоряет процесс, если достаточно ОЗУ.">Использовать RAM-диск</label>
                </div>
                <div style="margin-top:15px;">
                    <label>Разрешение интро:</label>
                    <div>
                      <input type="radio" id="intro_fullhd" name="intro_resolution" value="fullhd"> <label for="intro_fullhd">FullHD (1080p)</label>
                      <input type="radio" id="intro_2k" name="intro_resolution" value="2k" checked> <label for="intro_2k">2K (1440p)</label>
                    </div>
                </div>
            </fieldset>
            <fieldset><legend>2. Файлы</legend>
                <button type="button" onclick="selectFile('intro_file')">1. Выбрать исходник интро (если нужно)</button><div id="intro_file_path" class="file-path-display"></div>
                <button type="button" onclick="selectFile('video1')">2. Выбрать Видео 1</button><div id="video1_path" class="file-path-display"></div>
                <div id="video2_container" class="hidden"><button type="button" onclick="selectFile('video2')">3. Выбрать Видео 2</button><div id="video2_path" class="file-path-display"></div></div>
                <button type="button" onclick="selectFile('audio1')">Аудио 1 (опция)...</button><div id="audio1_path" class="file-path-display"></div>
                <div id="audio2_container" class="hidden"><button type="button" onclick="selectFile('audio2')">Аудио 2 (опция)...</button><div id="audio2_path" class="file-path-display"></div></div>
            </fieldset>
             <fieldset><legend>3. Таймкоды</legend>
                <h4>Сегмент 1</h4>
                <div class="time-input-group"><label for="start1" style="width:60px;">Начало:</label> <input type="text" id="start1" placeholder="00:01:10"><button class="jump-btn" onclick="jumpToTime('start1')">▶</button></div>
                <div class="time-input-group"><label for="end1" style="width:60px;">Конец:</label> <input type="text" id="end1" placeholder="00:45:30"><button class="jump-btn" onclick="jumpToTime('end1')">▶</button></div>
                <div id="part2_timecodes" class="hidden"><hr style="border-color: var(--panel-bg);"><h4>Сегмент 2</h4>
                    <div class="time-input-group"><label for="start2" style="width:60px;">Начало:</label> <input type="text" id="start2" placeholder="00:00:15"><button class="jump-btn" onclick="jumpToTime('start2')">▶</button></div>
                    <div class="time-input-group"><label for="end2" style="width:60px;">Конец:</label> <input type="text" id="end2" placeholder="00:30:00"><button class="jump-btn" onclick="jumpToTime('end2')">▶</button></div>
                </div>
            </fieldset>
            <button type="button" id="submitBtn" class="action-btn" onclick="submitForm()">Начать Обработку</button>
        </div>
        <div class="preview-column"><h2>Предпросмотр</h2><video id="videoPreview" controls></video><h2>Лог выполнения</h2><pre id="log-output">Ожидание подключения к серверу...</pre></div>
    </div>
    <script>
        const logOutput = document.getElementById('log-output');
        let filePaths = { video1: '', video2: '', audio1: '', audio2: '', intro_file: '' };
        let ws;
        function hmsToSeconds(str){if(!str)return 0;const p=str.split(':').map(Number);let s=0;if(p.length===3)s=p[0]*3600+p[1]*60+p[2];else if(p.length===2)s=p[0]*60+p[1];else if(p.length===1&&str)s=parseFloat(str);return isNaN(s)?0:s}
        function jumpToTime(id){const i=document.getElementById(id),t=hmsToSeconds(i.value),v=document.getElementById('videoPreview');if(!isNaN(t)&&v.duration){v.currentTime=t;v.play()}}

        function connect() {
            ws = new WebSocket(`ws://127.0.0.1:${SERVER_PORT}`);
            ws.onopen = () => { logOutput.textContent = 'Соединение установлено. Готов к работе.\\n'; };
            ws.onerror = () => { logOutput.textContent = 'Ошибка соединения WebSocket. Повторная попытка через 2 секунды...\\n'; };
            ws.onclose = () => { setTimeout(connect, 2000); };
            ws.onmessage = (event) => {
                const data = JSON.parse(event.data);
                if (data.action === 'file_selected') {
                    if (data.path) {
                        filePaths[data.id] = data.path;
                        document.getElementById(data.id + '_path').textContent = data.path;
                        if (data.id === 'video1') { document.getElementById('videoPreview').src = 'file://' + data.path; }
                    }
                } else if (data.action === 'log') {
                    if (data.message.startsWith('frame=')) {
                        const lines = logOutput.textContent.split('\\n');
                        if (lines.length > 1 && lines[lines.length - 2].startsWith('frame=')) {
                            lines[lines.length - 2] = data.message;
                            logOutput.textContent = lines.join('\\n');
                        } else {
                            logOutput.textContent += data.message + '\\n';
                        }
                    } else {
                        logOutput.textContent += data.message + '\\n';
                    }
                    logOutput.scrollTop = logOutput.scrollHeight;
                } else if (data.action === 'finished') { document.getElementById('submitBtn').disabled = false; document.getElementById('submitBtn').textContent = 'Начать Обработку'; }
            };
        }
        window.onload = connect;
        function selectFile(id) { if(ws && ws.readyState === WebSocket.OPEN){ ws.send(JSON.stringify({ action: 'select_file', id: id })); } else { logOutput.textContent += '\\nОшибка: нет соединения.\\n'; } }
        
        // ИЗМЕНЕНИЕ: Упрощенная и корректная логика
        function toggleMode() {
            const isSingleFileMode = document.getElementById('singleFile').checked;
            const isSingleSegmentMode = document.getElementById('is_single_segment').checked;
            
            // Показываем/скрываем поля для второго ИСТОЧНИКА (видео и аудио)
            document.getElementById('video2_container').style.display = isSingleFileMode ? 'none' : 'block';
            document.getElementById('audio2_container').style.display = isSingleFileMode ? 'none' : 'block';
            
            // Чекбокс "единый ролик" имеет смысл только в режиме одного файла
            document.getElementById('single_segment_toggle_div').style.display = isSingleFileMode ? 'block' : 'none';
            
            // Поля для второго СЕГМЕНТА (таймкоды) скрываются только если это один файл И один сегмент
            const showSecondTimecodes = !isSingleFileMode || (isSingleFileMode && !isSingleSegmentMode);
            document.getElementById('part2_timecodes').style.display = showSecondTimecodes ? 'block' : 'none';
        }

        function submitForm() {
            if (!ws || ws.readyState !== WebSocket.OPEN) { logOutput.textContent += '\\nОшибка: нет соединения.\\n'; return; }
            if (!filePaths.video1) { alert("Пожалуйста, выберите Видео 1"); return; }
            document.getElementById('submitBtn').disabled = true; document.getElementById('submitBtn').textContent = 'Обработка...'; logOutput.textContent = 'Запускаем...\\n';
            const params = {
                use_ram: document.getElementById('use_ram').checked,
                mode: document.querySelector('input[name="mode"]:checked').value,
                is_single_segment: document.getElementById('is_single_segment').checked,
                intro_resolution: document.querySelector('input[name="intro_resolution"]:checked').value,
                start1: document.getElementById('start1').value, end1: document.getElementById('end1').value,
                start2: document.getElementById('start2').value, end2: document.getElementById('end2').value,
                ...filePaths
            };
            ws.send(JSON.stringify({ action: 'process', params: params }));
        }
        toggleMode();
    </script>
</body>
</html>
""".replace("${SERVER_PORT}", str(SERVER_PORT))

# ==============================================================================
# ---                        ЛОГИКА PYTHON СЕРВЕРА                           ---
# ==============================================================================
def open_file_dialog():
    root = Tk(); root.withdraw(); root.attributes("-topmost", True); file_path = filedialog.askopenfilename(); root.destroy(); return file_path

async def send_log(websocket, message):
    try: await websocket.send(json.dumps({"action": "log", "message": message}))
    except websockets.exceptions.ConnectionClosed: pass

async def run_async_command(websocket, command, title=""):
    if title: await send_log(websocket, f"--- {title} ---")
    
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )
    
    buffer = ""
    while True:
        chunk = await process.stdout.read(128)
        if not chunk:
            break
        
        decoded_chunk = buffer + chunk.decode(errors='ignore')
        lines = decoded_chunk.replace('\r', '\n').split('\n')
        
        buffer = lines.pop()
        
        for line in lines:
            stripped_line = line.strip()
            if stripped_line:
                await send_log(websocket, stripped_line)

    if buffer.strip():
        await send_log(websocket, buffer.strip())

    await process.wait()
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, " ".join(map(str, command)))


async def handle_processing(websocket, params):
    temp_files_to_clean = []
    try:
        tmp_dir = "/dev/shm" if params.get('use_ram') and os.path.exists('/dev/shm') else '.'
        await send_log(websocket, "--- Этап 1: Подготовка данных ---")
        
        intro_resolution = params.get('intro_resolution', '2k')
        await send_log(websocket, f"Выбрано разрешение интро: {intro_resolution}")

        intro_path = os.path.realpath(os.path.join(DEFAULT_INTRO_DIR, f"{INTRO_BASE_NAME}_{intro_resolution}.mkv"))
        if not os.path.exists(intro_path):
            source_intro = params.get('intro_file') or next((os.path.realpath(pth) for pth in [os.path.join(DEFAULT_INTRO_DIR, f"{INTRO_BASE_NAME}.mp4"), os.path.join(DEFAULT_INTRO_DIR, f"{INTRO_BASE_NAME}.mkv")] if os.path.exists(pth)), None)
            if not source_intro: raise FileNotFoundError("Исходник интро не найден и не был выбран!")
            scale = "scale=2560:1440" if intro_resolution == '2k' else "scale=1920:1080"
            await run_async_command(websocket, ['ffmpeg','-hide_banner','-loglevel', 'error', '-i', source_intro, '-vf', scale, '-c:v', VIDEO_ENCODER, '-preset', 'medium', '-c:a', 'copy', intro_path, '-y'], f"Создание интро {intro_resolution}")
        
        segments = []
        is_single_segment = params.get('is_single_segment') and params.get('mode') == 'single'
        
        if not params.get('video1'): raise ValueError("Не указан Видеофайл 1.")
        
        def hms_to_seconds(time_str):
            if not time_str: return 0
            parts = str(time_str).split(':'); s = 0.0
            try:
                if len(parts) == 3: s = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                elif len(parts) == 2: s = int(parts[0]) * 60 + float(parts[1])
                elif len(parts) == 1 and time_str: s = float(time_str)
            except ValueError: s = 0.0
            return s
            
        segments.append({
            'video_orig': params['video1'], 'audio_orig': params.get('audio1') or params['video1'],
            'start': hms_to_seconds(params['start1']), 'end': hms_to_seconds(params['end1'])
        })

        if not is_single_segment:
            # ИЗМЕНЕНИЕ: В режиме одного файла, audio2 по умолчанию будет равно audio1 (или video1)
            video2_path = params['video1'] if params['mode'] == 'single' else params.get('video2')
            if not video2_path: raise ValueError("Не указан Видеофайл 2 для режима двух файлов.")
            
            audio2_source = params.get('audio2') or video2_path
            if params['mode'] == 'single' and not params.get('audio2'):
                audio2_source = params.get('audio1') or video2_path # Если audio2 не задан в режиме 1 файла, используем audio1

            segments.append({
                'video_orig': video2_path, 
                'audio_orig': audio2_source,
                'start': hms_to_seconds(params['start2']), 
                'end': hms_to_seconds(params['end2'])
            })
        
        video_concat_parts = [f"file '{intro_path}'"]
        audio_filter_definitions = []
        audio_concat_inputs = "[1:a]"
        ffmpeg_audio_inputs = ['-i', intro_path]

        for i, seg in enumerate(segments):
            await send_log(websocket, f"\n--- Обработка сегмента {i+1} ---")
            
            seg['video'] = seg['video_orig']
            if not seg['video'].lower().endswith('.mkv'):
                base, _ = os.path.splitext(seg['video']); sanitized = f"{base}_sanitized.mkv"
                if not os.path.exists(sanitized):
                    await run_async_command(websocket, ['ffmpeg','-hide_banner','-loglevel', 'error','-stats','-i', seg['video'], '-c', 'copy', sanitized, '-y'], f"Переупаковка '{os.path.basename(seg['video'])}'")
                seg['video'] = sanitized
            
            seg['audio'] = seg['audio_orig']
            if seg['audio_orig'] == seg['video_orig'] and seg['video'] != seg['video_orig']: seg['audio'] = seg['video']
                 
            cache_file = f"{seg['video']}.keyframes.txt"
            if not os.path.exists(cache_file):
                await send_log(websocket, f"Создание кэша I-кадров для {os.path.basename(seg['video'])}...")
                process = await asyncio.create_subprocess_exec('ffprobe','-v','error','-select_streams','v:0','-show_entries','packet=pts_time,flags','-of','csv=p=0', seg['video'], stdout=subprocess.PIPE)
                keyframes_data_bytes, _ = await process.communicate()
                with open(cache_file, 'w') as f: f.write('\n'.join([l.split(',')[0] for l in keyframes_data_bytes.decode().strip().split('\n') if ',K' in l]))

            with open(cache_file, 'r') as f: keyframes = [float(t) for t in f.read().strip().split('\n') if t]
            seg['start_split'] = next((t for t in keyframes if t > seg['start'] + FADE_DURATION), None)
            seg['end_split'] = [t for t in keyframes if t < seg['end'] - FADE_DURATION][-1] if any(t < seg['end'] - FADE_DURATION for t in keyframes) else None
            if not seg['start_split'] or not seg['end_split']: raise ValueError(f"Не найдены точки разделения для сегмента {i+1}. Сегмент слишком короткий или неверные таймкоды.")
            await send_log(websocket, f"Точки разделения: {seg['start_split']} -> {seg['end_split']}")
            
            seg['fout_rel'] = seg['end'] - seg['end_split'] - FADE_DURATION
            fade_in_path = os.path.join(tmp_dir, f"part{i+1}_fade_in.mkv"); temp_files_to_clean.append(fade_in_path)
            fade_out_path = os.path.join(tmp_dir, f"part{i+1}_fade_out.mkv"); temp_files_to_clean.append(fade_out_path)
            await run_async_command(websocket, ['ffmpeg','-hide_banner','-loglevel', 'error','-stats','-ss', str(seg['start']), '-to', str(seg['start_split']), '-i', seg['video'], '-an', '-vf', f"fade=in:st=0:d={FADE_DURATION},setpts=PTS-STARTPTS", '-c:v', VIDEO_ENCODER, '-preset', 'ultrafast', fade_in_path, '-y'], "Создание fade-in")
            await run_async_command(websocket, ['ffmpeg','-hide_banner','-loglevel', 'error','-stats','-ss', str(seg['end_split']), '-to', str(seg['end']), '-i', seg['video'], '-an', '-vf', f"fade=out:st={seg['fout_rel']:.4f}:d={FADE_DURATION},setpts=PTS-STARTPTS", '-c:v', VIDEO_ENCODER, '-preset', 'ultrafast', fade_out_path, '-y'], "Создание fade-out")

            video_concat_parts.extend([f"file '{fade_in_path}'", f"file '{seg['video']}'\ninpoint {seg['start_split']}\noutpoint {seg['end_split']}", f"file '{fade_out_path}'"])
            ain_idx = 1 + 1 + i
            ffmpeg_audio_inputs.extend(['-i', seg['audio']])
            audio_filter_definitions.extend([
                f"[{ain_idx}:a]asplit=3[aud{i}s1][aud{i}s2][aud{i}s3]",
                f"[aud{i}s1]atrim=start={seg['start']}:end={seg['start_split']},asetpts=PTS-STARTPTS,afade=t=in:st=0:d={FADE_DURATION}[aud{i}fi]",
                f"[aud{i}s2]atrim=start={seg['start_split']}:end={seg['end_split']},asetpts=PTS-STARTPTS[aud{i}mb]",
                f"[aud{i}s3]atrim=start={seg['end_split']}:end={seg['end']},asetpts=PTS-STARTPTS,afade=t=out:st={seg['fout_rel']:.4f}:d={FADE_DURATION}[aud{i}fo]"
            ])
            audio_concat_inputs += f"[aud{i}fi][aud{i}mb][aud{i}fo]"

        await send_log(websocket, "\n--- Этап 3: Финальная сборка ---")
        concat_path = os.path.join(tmp_dir, "concat.txt"); temp_files_to_clean.append(concat_path)
        with open(concat_path, 'w', encoding='utf-8') as f: f.write("\n".join(video_concat_parts))

        num_audio_filter_outputs = 1 + (len(segments) * 3)
        final_concat_filter = f"{audio_concat_inputs}concat=n={num_audio_filter_outputs}:v=0:a=1[fa]"
        filter_complex = ";".join(audio_filter_definitions + [final_concat_filter])
        
        base_name, _ = os.path.splitext(os.path.basename(segments[0]['video_orig']))
        
        res_prefix = params.get('intro_resolution', '2k')
        
        output_name = f"{res_prefix}_{base_name.replace('_sanitized','').replace('part1','')}_final_edit.mkv"
        output_dir = os.path.dirname(params['video1'])
        output_path = os.path.join(output_dir, output_name)

        final_cmd = ['ffmpeg','-hide_banner','-loglevel','error','-stats','-f','concat','-safe','0','-i', concat_path] + ffmpeg_audio_inputs + ['-filter_complex', filter_complex, '-map','0:v','-map','[fa]', '-c:v','copy','-r','60', '-c:a', FINAL_AUDIO_CODEC, output_path,'-y']
        
        await run_async_command(websocket, final_cmd, "Запускаем финальную сборку")
        await send_log(websocket, f"\nУСПЕХ! Финальный файл сохранен: {output_path}")

    except Exception as e:
        import traceback
        await send_log(websocket, f"\n\nКРИТИЧЕСКАЯ ОШИБКА: {str(e)}\n{traceback.format_exc()}\n")
    finally:
        await send_log(websocket, "\n--- Очистка ---")
        for f in temp_files_to_clean:
            if os.path.exists(f): 
                try:
                    os.remove(f)
                    await send_log(websocket, f"Удалено: {f}")
                except OSError as e:
                    await send_log(websocket, f"Не удалось удалить {f}: {e}")
        await websocket.send(json.dumps({"action": "finished"}))


async def handler(websocket):
    print("Клиент WebSocket подключен.")
    try:
        async for message in websocket:
            data = json.loads(message)
            action = data.get("action")
            if action == "select_file":
                file_path = await asyncio.to_thread(open_file_dialog)
                await websocket.send(json.dumps({ "action": "file_selected", "id": data.get("id"), "path": file_path }))
            elif action == "process":
                asyncio.create_task(handle_processing(websocket, data.get("params", {})))
    except websockets.exceptions.ConnectionClosed:
        print("Клиент отключился.")

async def main():
    try: 
        import tkinter
    except ImportError: 
        print("\nКРИТИЧЕСКАЯ ОШИБКА: 'tkinter' не найден. Установите его (обычно 'sudo apt-get install python3-tk' или 'pacman -S tk').")
        exit(1)
    
    async def http_server_handler(path, request_headers):
        if "Upgrade" in request_headers and request_headers["Upgrade"].lower() == "websocket":
            return None
        print(f"Отдаю HTML страницу для пути: {path}")
        headers = {"Content-Type": "text/html; charset=utf-8"}
        return (http.HTTPStatus.OK, headers, HTML_CONTENT.encode())

    async with serve(handler, "127.0.0.1", SERVER_PORT, process_request=http_server_handler):
        url = f"http://127.0.0.1:{SERVER_PORT}"
        print(f"Сервер запущен. Откройте эту ссылку в браузере: {url}")
        webbrowser.open_new_tab(url)
        await asyncio.Future()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nПриложение остановлено.")
    except OSError as e:
        if e.errno == 98:
            print(f"\nКРИТИЧЕСКАЯ ОШИБКА: Порт {SERVER_PORT} уже занят. Другая копия скрипта уже запущена?")
        else:
            print(f"Системная ошибка: {e}")
        exit(1)