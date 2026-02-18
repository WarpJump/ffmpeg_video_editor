## @file renderer.py
# @brief Модуль выполнения команд FFmpeg (Preview и Final Render).

import os
import uuid
import asyncio
import subprocess
import re
from typing import List, Tuple, Optional
from config import *
from timeline import VideoClip, analyze_keyframes, get_video_info

## @brief Отправляет лог на фронтенд и в системный журнал.
async def send_log(websocket, message: str, to_terminal: bool = True, msg_type: str = "log"):
    try: 
        await websocket.send(json.dumps({ "action": "log", "message": message, "type": msg_type }))
        if to_terminal: log_debug(f"[UI-LOG] {message}", to_console=True)
    except: pass

def seconds_to_hms(seconds: float) -> str:
    h = int(seconds // 3600); m = int((seconds % 3600) // 60); s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:05.2f}"

## @brief Обертка для запуска процесса FFmpeg.
# @details Читает вывод stdout/stderr, пишет его в лог-файл и парсит прогресс (time=...) для отправки в UI.
async def run_async_command(websocket, command: List[str], title: str = "", is_preview: bool = False, preview_time: float = 0.0):
    if title: await send_log(websocket, f"--- {title} ---", msg_type="header")
    cmd = [str(x) for x in command]
    
    log_debug(f"[CMD] START: {' '.join(cmd)}", to_console=False)
    
    with open(FFMPEG_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*20} {title.upper()} {'='*20}\nCOMMAND: {' '.join(cmd)}\n")

    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    
    if is_preview:
        await send_log(websocket, f"Рендеринг...", to_terminal=False, msg_type="progress")

    while True:
        chunk = await process.stdout.read(1024)
        if not chunk: break
        decoded = chunk.decode('utf-8', errors='ignore')
        
        with open(FFMPEG_LOG_FILE, "a", encoding="utf-8") as f: f.write(decoded)

        # Парсим прогресс только если это не превью (чтобы не забивать UI), или если очень нужно
        if not is_preview and ("frame=" in decoded or "time=" in decoded):
            time_m = re.search(r'time=([\d:.]+)', decoded)
            if time_m:
                cur_time = time_m.group(1)
                await send_log(websocket, f"Обработка: {cur_time}", to_terminal=False, msg_type="progress")

    await process.wait()
    if process.returncode != 0:
        log_debug(f"[CMD] ERROR: Code {process.returncode}")
        await send_log(websocket, f"\n[!] ОШИБКА FFmpeg", msg_type="log")
        raise subprocess.CalledProcessError(process.returncode, " ".join(cmd))
    else:
        log_debug(f"[CMD] SUCCESS")

## @brief Генерирует фрагмент видео для предпросмотра.
# @param request_time Время начала фрагмента на глобальном таймлайне.
# @return Tuple(путь_к_файлу, фактическое_время_начала).
async def render_preview_chunk(websocket, timeline: List[VideoClip], request_time: float) -> Tuple[Optional[str], float]:
    tmp_dir = "/dev/shm" if os.path.exists("/dev/shm") else "."
    
    log_debug(f"[PREVIEW] Запрос: {request_time:.2f}s")
    
    # 1. Определяем, какие клипы попадают в запрашиваемый интервал (request_time + 10s)
    req_end = request_time + FRAGMENT_DURATION
    active_clips = [c for c in timeline if (c.global_start + c.duration) > request_time and c.global_start < req_end]
    if not active_clips: 
        log_debug("[PREVIEW] Клипы не найдены")
        return None, request_time

    out_file = os.path.join(tmp_dir, f"tc_{str(uuid.uuid4())}.mkv")
    inputs, filters, v_pads, a_pads = [], [], [], []
    
    # 2. Строим фильтрграф ффмпега для склейки кусков
    for i, c in enumerate(active_clips):
        # Вычисляем пересечение времени клипа и времени запроса
        t_overlap_start = max(request_time, c.global_start)
        t_overlap_end = min(req_end, c.global_start + c.duration)
        needed_dur = t_overlap_end - t_overlap_start
        if needed_dur <= 0.05: continue # Пропускаем микро-куски

        offset_in_src = (t_overlap_start - c.global_start) + c.source_start
        seek = max(0, offset_in_src - SEEK_BUFFER)
        
        inputs.extend(['-ss', f"{seek:.4f}", '-i', c.source])
        
        # Точная подрезка (Trim) после seek
        trim_start = offset_in_src - seek
        
        # Масштабируем до 480p для скорости
        v_base = f"[{i}:v]scale={PREVIEW_WIDTH}:{PREVIEW_HEIGHT}:force_original_aspect_ratio=decrease,pad={PREVIEW_WIDTH}:{PREVIEW_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,trim=start={trim_start:.4f}:duration={needed_dur:.4f},setpts=PTS-STARTPTS"
        
        # Если это Fade-клип, и мы находимся в его временной зоне, накладываем эффект
        if c.is_fade:
            rel_start = t_overlap_start - c.global_start
            # Логика: если начало нашего куска совпадает с началом клипа, делаем Fade In
            if rel_start < FADE_DURATION: v_base += f",fade=in:st=0:d={FADE_DURATION}"
            # Если конец куска совпадает с концом клипа, делаем Fade Out
            # Примечание: для превью это упрощено
            if (c.duration - (t_overlap_start - c.global_start + needed_dur)) < FADE_DURATION:
                v_base += f",fade=out:st={max(0, needed_dur-FADE_DURATION):.4f}:d={FADE_DURATION}"

        filters.append(f"{v_base}[v{i}]")
        filters.append(f"[{i}:a]atrim=start={trim_start:.4f}:duration={needed_dur:.4f},asetpts=PTS-STARTPTS,volume={c.volume}[a{i}]")
        v_pads.append(f"[v{i}]"); a_pads.append(f"[a{i}]")

    # Склеиваем все подготовленные куски
    filters.append(f"{''.join(v_pads)}concat=n={len(v_pads)}:v=1:a=0[v_out]")
    filters.append(f"{''.join(a_pads)}concat=n={len(a_pads)}:v=0:a=1[a_out]")

    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'info', '-stats'] + inputs + \
          ['-filter_complex', ";".join(filters), '-map', '[v_out]', '-map', '[a_out]',
           '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28', '-c:a', 'aac', '-b:a', '128k', out_file, '-y']
    
    await run_async_command(websocket, cmd, f"TRANSCODING ({seconds_to_hms(request_time)})", is_preview=True)
    return out_file, request_time

## @class FinalRenderer
# @brief Выполняет финальный экспорт видео в максимальном качестве.
# @details Использует гибридный подход для скорости и качества:
# 1. Интро и эффекты (Fade) перекодируются в формат основного видео.
# 2. Основная часть (Body) склеивается без перекодирования (Concat Demuxer).
# 3. Аудио собирается отдельным графом, чтобы обеспечить кроссфейды и громкость без рассинхрона.
class FinalRenderer:
    def __init__(self, websocket, timeline: List[VideoClip], output_path: str):
        self.ws = websocket; self.timeline = timeline; self.output_path = output_path
        self.tmp_dir = "/dev/shm" if os.path.exists("/dev/shm") else "."; self.temp_files = []

    async def render(self):
        log_debug("[FINAL] Старт рендера")
        # Берем параметры первого видео-сегмента как эталон
        v1_clip = next((c for c in self.timeline if "Seg" in c.name), self.timeline[0])
        master = await get_video_info(v1_clip.source)
        
        # Фаза 1: Адаптация (Pre-render)
        for i, c in enumerate(self.timeline):
            needs_reencode = False
            if c.is_fade: needs_reencode = True
            elif c.name == "Intro":
                # Проверяем, совпадает ли интро по параметрам с основным видео
                intro_info = await get_video_info(c.source)
                if intro_info.get('width') != master['width'] or intro_info.get('fps') != master['fps']:
                    needs_reencode = True
            
            if needs_reencode:
                log_debug(f"[FINAL] Адаптация {c.name}")
                out = os.path.join(self.tmp_dir, f"compat_{i}_{str(uuid.uuid4())[:4]}.mkv"); self.temp_files.append(out)
                # Рендерим кусок, приводя его к мастер-формату
                cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'info', '-stats', '-ss', f"{c.source_start:.4f}", '-i', c.source, '-t', f"{c.duration:.4f}"]
                vf = [f"scale={master['width']}:{master['height']}:force_original_aspect_ratio=decrease", f"pad={master['width']}:{master['height']}:(ow-iw)/2:(oh-ih)/2", f"fps={master['fps']}", "setsar=1"] + c.video_filters + ["setpts=PTS-STARTPTS"]
                af = [f"volume={c.volume}"] + c.audio_filters + ["asetpts=PTS-STARTPTS"]
                cmd.extend(['-filter_complex', f"[0:v]{','.join(vf)}[v];[0:a]{','.join(af)}[a]", '-map', '[v]', '-map', '[a]', '-c:v', VIDEO_ENCODER, '-preset', 'ultrafast', '-crf', '18', '-c:a', FINAL_AUDIO_CODEC, out, '-y'])
                await run_async_command(self.ws, cmd, f"Адаптация фрагмента: {c.name}")
                
                # Обновляем клип в таймлайне, чтобы он ссылался на новый файл
                c.source, c.audio_source, c.source_start, c.is_fade, c.video_filters, c.audio_filters = out, out, 0.0, False, [], []
        
        # Фаза 2: Concat Video (Склейка без перекодирования)
        list_file = os.path.join(self.tmp_dir, "concat.txt"); self.temp_files.append(list_file)
        with open(list_file, 'w') as f:
            for c in self.timeline:
                f.write(f"file '{c.source}'\n"); f.write(f"inpoint {c.source_start:.4f}\noutpoint {c.source_start+c.duration:.4f}\n")
        
        # Фаза 3: Mix Audio (Финальная сборка)
        # Видео берем из concat-файла (stream copy), аудио собираем фильтрами
        cmd_inputs = ['-f', 'concat', '-safe', '0', '-i', list_file]
        a_filters = []
        for i, c in enumerate(self.timeline):
            cmd_inputs.extend(['-i', c.audio_source])
            f = [f"atrim=start={c.source_start:.4f}:duration={c.duration:.4f}", "asetpts=PTS-STARTPTS", f"volume={c.volume}"]
            a_filters.append(f"[{i+1}:a]{','.join(f)}[a{i}]")
        a_filters.append(f"{''.join(f'[a{i}]' for i in range(len(self.timeline)))}concat=n={len(self.timeline)}:v=0:a=1[aout]")
        
        final_cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'info', '-stats'] + cmd_inputs + ['-filter_complex', ";".join(a_filters), '-map', '0:v', '-map', '[aout]', '-c:v', 'copy', '-c:a', FINAL_AUDIO_CODEC, self.output_path, '-y']
        
        await run_async_command(self.ws, final_cmd, "Финальная склейка")
        # Очистка временных файлов
        for f in self.temp_files: 
            if os.path.exists(f): 
                try: os.remove(f)
                except: pass