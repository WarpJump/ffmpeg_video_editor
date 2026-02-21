## @file renderer.py
# @brief Модуль выполнения команд FFmpeg (Гибридный VAAPI + CPU).

import os
import uuid
import asyncio
import subprocess
import re
import json
from typing import List, Tuple, Optional
from config import *
from timeline import VideoClip, get_video_info

# --- Глобальное состояние железа ---
HW_INFO = {
    "vaapi_supported": False,
    "vaapi_overlay": False,
    "device": "/dev/dri/renderD128"
}

async def detect_hw_support():
    """ Проверка поддержки VAAPI (Масштабирование и Наложение) """
    log_debug(f"[HW] Тестирование аппаратного ускорения VAAPI ({HW_INFO['device']})...")
    
    # ТЕСТ 1: Поддержка базового VAAPI (hwupload + scale_vaapi + h264_vaapi)
    test1_cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        '-init_hw_device', f'vaapi=va:{HW_INFO["device"]}', '-filter_hw_device', 'va',
        '-f', 'lavfi', '-i', 'color=black:s=128x128:r=24',
        '-vf', 'format=nv12,hwupload,scale_vaapi=w=128:h=128',
        '-c:v', 'h264_vaapi', '-frames:v', '1', '-f', 'null', '-'
    ]
    try:
        proc1 = await asyncio.create_subprocess_exec(*test1_cmd)
        await proc1.wait()
        if proc1.returncode == 0:
            HW_INFO["vaapi_supported"] = True
            log_debug("[HW] Базовый VAAPI (Scale + Encode) УСПЕШНО активирован.")
            
            # ТЕСТ 2: Поддержка overlay_vaapi (На некоторых iGPU AMD не работает)
            test2_cmd = [
                'ffmpeg', '-hide_banner', '-loglevel', 'error',
                '-init_hw_device', f'vaapi=va:{HW_INFO["device"]}', '-filter_hw_device', 'va',
                '-f', 'lavfi', '-i', 'color=black:s=128x128:r=24',
                '-f', 'lavfi', '-i', 'color=white:s=128x128:r=24',
                '-filter_complex', '[0:v]format=nv12,hwupload[bg];[1:v]format=nv12,hwupload[fg];[bg][fg]overlay_vaapi=x=0:y=0',
                '-c:v', 'h264_vaapi', '-frames:v', '1', '-f', 'null', '-'
            ]
            print(" ".join(test2_cmd))
            proc2 = await asyncio.create_subprocess_exec(*test2_cmd)
            await proc2.wait()
            if proc2.returncode == 0:
                HW_INFO["vaapi_overlay"] = True
                log_debug("[HW] Аппаратное наложение (overlay_vaapi) ПОДДЕРЖИВАЕТСЯ.")
            else:
                log_debug("[HW] Аппаратное наложение НЕ поддерживается. Включен ГИБРИДНЫЙ режим (CPU Overlay).")
        else:
            log_debug("[HW] VAAPI не поддерживается или ошибка драйвера. Используем CPU.")
    except Exception as e:
        log_debug(f"[HW] Ошибка детекции VAAPI: {e}")

def seconds_to_hms(seconds: float) -> str:
    h = int(seconds // 3600); m = int((seconds % 3600) // 60); s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:05.2f}"

def get_scale_pad_filter(width: int, height: int, sar: str = "1") -> str:
    """ Возвращает цепочку фильтров для CPU: Scale + Pad + SetSAR """
    return f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar={sar}"

def get_vaapi_scale_pad_hybrid(width: int, height: int, sar: str = "1") -> str:
    """ 
    Возвращает цепочку фильтров для VAAPI: 
    GPU Scale (с сохранением AR) -> Download -> CPU Pad (черные полосы) -> SetSAR 
    """
    # 1. scale_vaapi с force_original_aspect_ratio=decrease впишет видео в прямоугольник, не нарушая пропорций.
    # 2. hwdownload возвращает кадр в RAM (он может быть меньше целевого размера, например 1440x1080).
    # 3. pad (софтварный) добавляет черные полосы до целевого размера (1920x1080) и центрирует.
    return (f"format=nv12,hwupload,"
            f"scale_vaapi=w={width}:h={height}:force_original_aspect_ratio=decrease,"
            f"hwdownload,format=nv12,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            f"setsar={sar}")

async def send_log(websocket, message: str, to_terminal: bool = True, msg_type: str = "log"):
    try: 
        await websocket.send(json.dumps({ "action": "log", "message": message, "type": msg_type }))
        if to_terminal: log_debug(f"[UI-LOG] {message}", to_console=True)
    except: pass

async def run_async_command(websocket, command: List[str], title: str = "", is_preview: bool = False):
    if title: await send_log(websocket, f"--- {title} ---", msg_type="header")
    cmd = [str(x) for x in command]
    
    with open(FFMPEG_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*20} {title.upper()} {'='*20}\nCOMMAND: {' '.join(cmd)}\n")

    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    if is_preview: await send_log(websocket, f"Рендеринг...", to_terminal=False, msg_type="progress")

    while True:
        chunk = await process.stdout.read(1024)
        if not chunk: break
        decoded = chunk.decode('utf-8', errors='ignore')
        with open(FFMPEG_LOG_FILE, "a", encoding="utf-8") as f: f.write(decoded)
        
        if not is_preview and ("frame=" in decoded or "time=" in decoded):
            time_m = re.search(r'time=([\d:.]+)', decoded)
            fps_m = re.search(r'fps=\s*([\d.]+)', decoded)
            if time_m:
                time_str = time_m.group(1)
                fps_str = fps_m.group(1) if fps_m else "??"
                await send_log(websocket, f"Обработка: {time_str} | Скорость: {fps_str} fps", to_terminal=False, msg_type="progress")

    await process.wait()
    if process.returncode != 0:
        await send_log(websocket, f"\n[!] ОШИБКА FFmpeg", msg_type="log")
        raise subprocess.CalledProcessError(process.returncode, " ".join(cmd))

async def render_preview_chunk(websocket, timeline: List[VideoClip], request_time: float) -> Tuple[Optional[str], float]:
    tmp_dir = "/dev/shm" if os.path.exists("/dev/shm") else "."
    req_end = request_time + FRAGMENT_DURATION
    active_clips = [c for c in timeline if (c.global_start + c.duration) > request_time and c.global_start < req_end]
    if not active_clips: return None, request_time

    out_file = os.path.join(tmp_dir, f"tc_{str(uuid.uuid4())}.mkv")
    
    inputs = []
    if HW_INFO["vaapi_supported"]:
        inputs.extend(['-init_hw_device', f'vaapi=va:{HW_INFO["device"]}', '-filter_hw_device', 'va'])

    filters, v_pads, a_pads = [], [], []
    in_idx = 0
    
    for i, c in enumerate(active_clips):
        t_start = max(request_time, c.global_start)
        t_end = min(req_end, c.global_start + c.duration)
        needed_dur = t_end - t_start
        if needed_dur <= 0.05: continue

        offset_in_src = (t_start - c.global_start) + c.source_start
        seek = max(0, offset_in_src - SEEK_BUFFER)
        trim_st = offset_in_src - seek
        
        inputs.extend(['-ss', f"{seek:.4f}", '-i', c.source])
        base_idx = in_idx; in_idx += 1
        
        # 1. Скейл ОСНОВЫ (Hybrid: GPU Scale + CPU Pad)
        if HW_INFO["vaapi_supported"]:
            # Используем новую функцию для умного ресайза с полосами
            v_base = f"[{base_idx}:v]{get_vaapi_scale_pad_hybrid(PREVIEW_WIDTH, PREVIEW_HEIGHT)}"
        else:
            v_base = f"[{base_idx}:v]{get_scale_pad_filter(PREVIEW_WIDTH, PREVIEW_HEIGHT)}"
        
        filters.append(f"{v_base},trim=start={trim_st:.4f}:duration={needed_dur:.4f},setpts=PTS-STARTPTS[base_v{i}]")
        cur_v = f"[base_v{i}]"

        if c.has_overlay:
            pip_offset = (t_start - c.global_start) + c.overlay_source_start
            pip_seek = max(0, pip_offset - SEEK_BUFFER)
            pip_trim = pip_offset - pip_seek
            
            inputs.extend(['-ss', f"{pip_seek:.4f}", '-i', c.overlay_source])
            pip_idx = in_idx; in_idx += 1
            
            # Для PiP (картинка в картинке) обычно не нужны черные полосы ВНУТРИ окошка PiP,
            # но setsar=1 нужен обязательно. Пока оставим просто setsar, чтобы окно заполнялось.
            # Если нужно сохранять AR и для PiP - можно применить ту же логику.
            pw = int(PREVIEW_WIDTH * c.overlay_w); ph = int(pw * (9/16))
            pw -= pw % 2; ph -= ph % 2
            px = int(PREVIEW_WIDTH * c.overlay_x); py = int(PREVIEW_HEIGHT * c.overlay_y)

            # 2. Скейл PiP
            if HW_INFO["vaapi_supported"]:
                pip_scale = f"[{pip_idx}:v]format=nv12,hwupload,scale_vaapi={pw}:{ph},hwdownload,format=nv12,setsar=1"
            else:
                pip_scale = f"[{pip_idx}:v]scale={pw}:{ph}:force_original_aspect_ratio=decrease,setsar=1"
            
            filters.append(f"{pip_scale},trim=start={pip_trim:.4f}:duration={needed_dur:.4f},setpts=PTS-STARTPTS[pip_v{i}]")

            # 3. Наложение (Overlay)
            if HW_INFO["vaapi_supported"] and HW_INFO["vaapi_overlay"]:
                filters.append(f"{cur_v}format=nv12,hwupload[bg_v{i}]")
                filters.append(f"[pip_v{i}]format=nv12,hwupload[fg_v{i}]")
                filters.append(f"[bg_v{i}][fg_v{i}]overlay_vaapi=x={px}:y={py},hwdownload,format=nv12,setsar=1[ovl_v{i}]")
            else:
                filters.append(f"{cur_v}[pip_v{i}]overlay=x={px}:y={py}:eof_action=pass[ovl_v{i}]")
            
            cur_v = f"[ovl_v{i}]"

        if c.is_fade:
            if c.fade_in and (t_start - c.global_start) < FADE_DURATION:
                filters.append(f"{cur_v}fade=in:st=0:d={FADE_DURATION}[fv_in{i}]"); cur_v = f"[fv_in{i}]"
            if c.fade_out and (c.duration - (t_start - c.global_start + needed_dur)) < FADE_DURATION:
                filters.append(f"{cur_v}fade=out:st={max(0, needed_dur-FADE_DURATION):.4f}:d={FADE_DURATION}[fv_out{i}]"); cur_v = f"[fv_out{i}]"
                
        v_pads.append(cur_v)
        filters.append(f"[{base_idx}:a]atrim=start={trim_st:.4f}:duration={needed_dur:.4f},asetpts=PTS-STARTPTS,volume={c.volume}[base_a{i}]")
        cur_a = f"[base_a{i}]"

        if c.has_overlay and c.overlay_audio:
            filters.append(f"[{pip_idx}:a]atrim=start={pip_trim:.4f}:duration={needed_dur:.4f},asetpts=PTS-STARTPTS,volume=1.0[pip_a{i}]")
            filters.append(f"{cur_a}[pip_a{i}]amix=inputs=2:duration=first:dropout_transition=2[mix_a{i}]"); cur_a = f"[mix_a{i}]"
        a_pads.append(cur_a)

    v_out_str = f"{''.join(v_pads)}concat=n={len(v_pads)}:v=1:a=0"
    if HW_INFO["vaapi_supported"]:
        v_out_str += ",format=nv12,hwupload[v_out]"
    else:
        v_out_str += "[v_out]"
    filters.append(v_out_str)
    
    filters.append(f"{''.join(a_pads)}concat=n={len(a_pads)}:v=0:a=1[a_out]")

    v_codec = 'h264_vaapi' if HW_INFO["vaapi_supported"] else 'libx264'
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'level+time+verbose'] + inputs + \
          ['-filter_complex', ";".join(filters), '-map', '[v_out]', '-map', '[a_out]',
           '-c:v', v_codec, '-preset', 'ultrafast', '-r', '20', '-crf', '28', '-g', '15', '-c:a', 'aac', '-b:a', '128k', out_file, '-y']
    
    await run_async_command(websocket, cmd, f"TRANSCODING ({seconds_to_hms(request_time)})", is_preview=True)
    return out_file, request_time

class FinalRenderer:
    def __init__(self, websocket, timeline: List[VideoClip], output_path: str, intro_res: str):
        self.ws = websocket; self.timeline = timeline; self.output_path = output_path
        self.tmp_dir = "/dev/shm" if os.path.exists("/dev/shm") else "."
        self.out_dir = os.path.dirname(output_path)
        self.intro_res_mode = intro_res
        self.temp_files = []

    async def render(self):
        log_debug("[FINAL] Старт рендера")
        v1_clip = next((c for c in self.timeline if "Seg" in c.name), self.timeline[0])
        master = await get_video_info(v1_clip.source)
        master_ar = master['width'] / master['height'] if master['height'] > 0 else 1.777
        
        for i, c in enumerate(self.timeline):
            needs_reencode = c.is_fade or c.has_overlay
            target_w, target_h = master['width'], master['height']
            
            if c.name == "Intro":
                target_h = 1440 if self.intro_res_mode == '2k' else 1080
                target_w = int(target_h * master_ar); target_w -= target_w % 2
                info = await get_video_info(c.source)
                if info.get('width') != target_w or info.get('fps') != master['fps']: needs_reencode = True
            
            if needs_reencode:
                save_dir = self.out_dir if c.duration > 60 else self.tmp_dir
                out = os.path.join(save_dir, f"compat_{i}_{str(uuid.uuid4())[:4]}.mkv"); self.temp_files.append(out)
                
                cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'info', '-stats']
                if HW_INFO["vaapi_supported"]:
                    cmd.extend(['-init_hw_device', f'vaapi=va:{HW_INFO["device"]}', '-filter_hw_device', 'va'])
                
                cmd.extend(['-ss', f"{c.source_start:.4f}", '-i', c.source])
                if c.has_overlay: cmd.extend(['-ss', f"{c.overlay_source_start:.4f}", '-i', c.overlay_source])
                cmd.extend(['-t', f"{c.duration:.4f}"])
                
                # Аналогичная гибридная логика для финального рендера
                if HW_INFO["vaapi_supported"]:
                    # FIX: Используем scale_vaapi + CPU pad для правильного AR
                    vf = [f"[0:v]{get_vaapi_scale_pad_hybrid(target_w, target_h)},fps={master['fps']}[base_v]"]
                    cur_v = "[base_v]"
                    if c.has_overlay:
                        ov_info = await get_video_info(c.overlay_source)
                        ov_ar = ov_info.get('width', 1920)/ov_info.get('height', 1080) if ov_info.get('height', 1)>0 else 1.777
                        pw = int(target_w * c.overlay_w); ph = int(pw / ov_ar); pw -= pw % 2; ph -= ph % 2
                        px = int(target_w * c.overlay_x); py = int(target_h * c.overlay_y)
                        
                        vf.append(f"[1:v]format=nv12,hwupload,scale_vaapi={pw}:{ph},hwdownload,format=nv12,setsar=1,fps={master['fps']}[pip_v]")
                        if HW_INFO["vaapi_overlay"]:
                            vf.append(f"{cur_v}format=nv12,hwupload[bg_v]")
                            vf.append(f"[pip_v]format=nv12,hwupload[fg_v]")
                            vf.append(f"[bg_v][fg_v]overlay_vaapi=x={px}:y={py},hwdownload,format=nv12,setsar=1[ovl_v]")
                        else:
                            vf.append(f"{cur_v}[pip_v]overlay=x={px}:y={py}:eof_action=pass[ovl_v]")
                        cur_v = "[ovl_v]"
                else:
                    vf = [f"[0:v]{get_scale_pad_filter(target_w, target_h)},fps={master['fps']}[base_v]"]
                    cur_v = "[base_v]"
                    if c.has_overlay:
                        ov_info = await get_video_info(c.overlay_source)
                        ov_ar = ov_info.get('width', 1920)/ov_info.get('height', 1080) if ov_info.get('height', 1)>0 else 1.777
                        pw = int(target_w * c.overlay_w); ph = int(pw / ov_ar); pw -= pw % 2; ph -= ph % 2
                        px = int(target_w * c.overlay_x); py = int(target_h * c.overlay_y)
                        vf.append(f"[1:v]scale={pw}:{ph}:force_original_aspect_ratio=decrease,setsar=1,fps={master['fps']}[pip_v]")
                        vf.append(f"{cur_v}[pip_v]overlay=x={px}:y={py}:eof_action=pass[ovl_v]")
                        cur_v = "[ovl_v]"

                if c.fade_in: vf.append(f"{cur_v}fade=in:st=0:d={FADE_DURATION}[fv_in]"); cur_v = "[fv_in]"
                if c.fade_out: vf.append(f"{cur_v}fade=out:st={c.duration-FADE_DURATION:.4f}:d={FADE_DURATION}[fv_out]"); cur_v = "[fv_out]"
                
                if HW_INFO["vaapi_supported"]:
                    vf.append(f"{cur_v}format=nv12,hwupload[out_v]")
                    cur_v = "[out_v]"
                
                af = [f"[0:a]volume={c.volume}[base_a]"]
                cur_a = "[base_a]"
                if c.has_overlay and c.overlay_audio:
                    af.append(f"[1:a]volume=1.0[pip_a]")
                    af.append(f"{cur_a}[pip_a]amix=inputs=2:duration=first:dropout_transition=2[mix_a]"); cur_a = "[mix_a]"
                if c.fade_in: af.append(f"{cur_a}afade=t=in:st=0:d={FADE_DURATION}[fa_in]"); cur_a = "[fa_in]"
                if c.fade_out: af.append(f"{cur_a}afade=t=out:st={c.duration-FADE_DURATION:.4f}:d={FADE_DURATION}[fa_out]"); cur_a = "[fa_out]"

                v_codec = 'h264_vaapi' if HW_INFO["vaapi_supported"] else VIDEO_ENCODER
                cmd.extend(['-filter_complex', f"{';'.join(vf)};{';'.join(af)}", '-map', cur_v, '-map', cur_a, 
                            '-c:v', v_codec, '-preset', 'ultrafast', '-crf', '18', '-c:a', FINAL_AUDIO_CODEC, out, '-y'])
                
                await run_async_command(self.ws, cmd, f"Адаптация: {c.name}")
                c.source, c.audio_source, c.source_start, c.is_fade, c.has_overlay = out, out, 0.0, False, False
        
        list_file = os.path.join(self.tmp_dir, "concat.txt"); self.temp_files.append(list_file)
        with open(list_file, 'w') as f:
            for c in self.timeline: f.write(f"file '{c.source}'\ninpoint {c.source_start:.4f}\noutpoint {c.source_start+c.duration:.4f}\n")
        
        cmd_inputs = ['-f', 'concat', '-safe', '0', '-i', list_file]
        a_filters = []
        for i, c in enumerate(self.timeline):
            cmd_inputs.extend(['-i', c.audio_source])
            a_filters.append(f"[{i+1}:a]atrim=start={c.source_start:.4f}:duration={c.duration:.4f},asetpts=PTS-STARTPTS,volume={c.volume}[a{i}]")
        a_filters.append(f"{''.join(f'[a{i}]' for i in range(len(self.timeline)))}concat=n={len(self.timeline)}:v=0:a=1[aout]")
        
        final_cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'info'] + cmd_inputs + \
                    ['-filter_complex', ";".join(a_filters), '-map', '0:v', '-map', '[aout]', '-c:v', 'copy', '-c:a', FINAL_AUDIO_CODEC, self.output_path, '-y']
        
        await run_async_command(self.ws, final_cmd, "Финальная склейка")
        for f in self.temp_files: 
            if os.path.exists(f): 
                try: os.remove(f)
                except: pass