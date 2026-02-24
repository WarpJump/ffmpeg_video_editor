## @file renderer.py
# @brief Модуль выполнения команд FFmpeg (CUDA + VAAPI + CPU).

import os
import uuid
import asyncio
import subprocess
import re
import json
from typing import List, Tuple, Optional
from config import *
from timeline import VideoClip, get_video_info

HW_INFO = {
    "engine": "cpu", # "cuda", "vaapi", "cpu"
    "device": "",
    "overlay_supported": False
}

async def run_hw_test(test_name: str, cmd: List[str], log_file: str) -> bool:
    """ Выполняет конкретную тестовую команду FFmpeg и пишет результат в лог """
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*10} TEST HW: {test_name} {'='*10}\nCOMMAND: {' '.join(cmd)}\n")

        process = await asyncio.create_subprocess_exec(
            *cmd, 
            stdout=asyncio.subprocess.PIPE, 
            stderr=asyncio.subprocess.STDOUT
        )
        stdout, _ = await process.communicate()
        decoded = stdout.decode('utf-8', errors='ignore')

        with open(log_file, "a", encoding="utf-8") as f:
            f.write(decoded)
            status = "SUCCESS" if process.returncode == 0 else f"FAILED (code {process.returncode})"
            f.write(f"\nRESULT: {status}\n{'-'*40}\n")

        return process.returncode == 0
    except Exception as e:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"CRITICAL ERROR DURING CHECK {test_name}: {e}\n")
        return False

async def detect_hw_support():
    log_debug(f"[HW] Детекция аппаратного ускорения (Логи в {FFMPEG_PREVIEW_LOG})...")
    
    with open(FFMPEG_PREVIEW_LOG, "a", encoding="utf-8") as f:
        f.write(f"\n\n{'#'*30}\n# HARDWARE DETECTION START\n{'#'*30}\n")

    cuda_cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'verbose',
        '-init_hw_device', 'cuda=cu:0', '-filter_hw_device', 'cu',
        '-f', 'lavfi', '-i', 'color=black:s=256x256:r=24',
        '-vf', 'format=nv12,hwupload_cuda,scale_cuda=256:256',
        '-c:v', 'h264_nvenc', '-frames:v', '1', '-f', 'null', '-'
    ]
    if await run_hw_test("NVIDIA CUDA", cuda_cmd, FFMPEG_PREVIEW_LOG):
        HW_INFO["engine"] = "cuda"
        HW_INFO["overlay_supported"] = True 
        log_debug("✅ Используем CUDA")
        return

    # ТЕСТ 2: VAAPI (Intel/AMD)
    vaapi_device = "/dev/dri/renderD128"
    if os.path.exists(vaapi_device):
        vaapi_cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'verbose',
            '-init_hw_device', f'vaapi=va:{vaapi_device}', '-filter_hw_device', 'va',
            '-f', 'lavfi', '-i', 'color=black:s=256x256:r=24',
            '-vf', 'format=nv12,hwupload,scale_vaapi=w=256:h=256',
            '-c:v', 'h264_vaapi', '-frames:v', '1', '-f', 'null', '-'
        ]
        if await run_hw_test("INTEL/AMD VAAPI", vaapi_cmd, FFMPEG_PREVIEW_LOG):
            HW_INFO["engine"] = "vaapi"
            HW_INFO["device"] = vaapi_device
            log_debug(f"✅ Используем VAAPI на {vaapi_device}.")
            
            ovl_cmd = [
                'ffmpeg', '-hide_banner', '-loglevel', 'verbose',
                '-init_hw_device', f'vaapi=va:{vaapi_device}', '-filter_hw_device', 'va', 
                '-f', 'lavfi', '-i', 'color=black:s=256x256:r=24',
                '-f', 'lavfi', '-i', 'color=white:s=256x256:r=24',
                '-filter_complex', '[0:v]format=nv12,hwupload[bg];[1:v]format=nv12,hwupload[fg];[bg][fg]overlay_vaapi=x=0:y=0',
                '-c:v', 'h264_vaapi', '-frames:v', '1', '-f', 'null', '-'
            ]
            HW_INFO["overlay_supported"] = await run_hw_test("VAAPI OVERLAY", ovl_cmd, FFMPEG_PREVIEW_LOG)
            return

    log_debug("⚠️ Аппаратное ускорение не найдено. Используем CPU.")
    with open(FFMPEG_PREVIEW_LOG, "a", encoding="utf-8") as f:
        f.write("\nFinal Decision: Using CPU (libx264)\n")

# --- Помощники фильтров ---
def calc_ar_dims(src_w: int, src_h: int, max_w: int, max_h: int) -> Tuple[int, int]:
    if src_w == 0 or src_h == 0: return max_w, max_h
    ratio = min(max_w / src_w, max_h / src_h)
    tw, th = int(src_w * ratio), int(src_h * ratio)
    return tw - (tw % 2), th - (th % 2)

def build_hw_scale_pad(engine: str, src_w: int, src_h: int, target_w: int, target_h: int) -> str:
    tw, th = calc_ar_dims(src_w, src_h, target_w, target_h)
    pad = f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2"
    
    if engine == "cuda":
        return f"format=nv12,hwupload_cuda,scale_cuda={tw}:{th}:format=nv12,hwdownload,format=nv12,{pad},setsar=1"
    elif engine == "vaapi":
        return f"format=nv12,hwupload,scale_vaapi=w={target_w}:h={target_h}:force_original_aspect_ratio=decrease,hwdownload,format=nv12,{pad},setsar=1"
    else:
        return f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,{pad},setsar=1"

def build_hw_scale_pip(engine: str, src_w: int, src_h: int, max_w: int, max_h: int) -> str:
    tw, th = calc_ar_dims(src_w, src_h, max_w, max_h)
    if engine == "cuda": return f"format=nv12,hwupload_cuda,scale_cuda={tw}:{th}:format=nv12,hwdownload,format=nv12,setsar=1"
    elif engine == "vaapi": return f"format=nv12,hwupload,scale_vaapi={tw}:{th},hwdownload,format=nv12,setsar=1"
    else: return f"scale={tw}:{th},setsar=1"

def build_hw_overlay(engine: str, bg_pad: str, fg_pad: str, x: int, y: int) -> List[str]:
    if engine == "cuda":
        return [f"{bg_pad}format=nv12,hwupload_cuda[bg_hw]", f"{fg_pad}format=nv12,hwupload_cuda[fg_hw]", 
                f"[bg_hw][fg_hw]overlay_cuda=x={x}:y={y},hwdownload,format=nv12"]
    elif engine == "vaapi":
        return [f"{bg_pad}format=nv12,hwupload[bg_hw]", f"{fg_pad}format=nv12,hwupload[fg_hw]", 
                f"[bg_hw][fg_hw]overlay_vaapi=x={x}:y={y},hwdownload,format=nv12"]
    else:
        return [f"{bg_pad}{fg_pad}overlay=x={x}:y={y}:eof_action=pass"]

def get_last_error(log_file: str, lines: int = 4) -> str:
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            content = f.readlines()
            errors = [line.strip() for line in content if "frame=" not in line and "fps=" not in line and line.strip()]
            return "\n".join(errors[-lines:])
    except: return "Не удалось прочитать подробности из лога."

def seconds_to_hms(seconds: float) -> str:
    h = int(seconds // 3600); m = int((seconds % 3600) // 60); s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:05.2f}"

async def send_log(websocket, message: str, to_terminal: bool = True, msg_type: str = "log"):
    try: 
        await websocket.send(json.dumps({ "action": "log", "message": message, "type": msg_type }))
        if to_terminal: log_debug(f"[UI-LOG] {message}", to_console=True)
    except: pass

async def run_async_command(websocket, command: List[str], log_file: str, title: str = "", is_preview: bool = False):
    if title: await send_log(websocket, f"--- {title} ---", msg_type="header")
    cmd = [str(x) for x in command]
    
    seek_msg = "(Fast Seek)" if '-ss' in cmd[:5] else ""
    log_debug(f"[FFmpeg] Start: {title} {seek_msg}")
    
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*20} {title.upper()} {'='*20}\nCOMMAND: {' '.join(cmd)}\n")

    start_time = asyncio.get_event_loop().time()
    process = None
    try:
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        
        while True:
            chunk = await process.stdout.read(1024)
            if not chunk: break
            decoded = chunk.decode('utf-8', errors='ignore')
            with open(log_file, "a", encoding="utf-8") as f: f.write(decoded)
            
            if not is_preview and ("frame=" in decoded or "time=" in decoded):
                time_m = re.search(r'time=([\d:.]+)', decoded)
                bitr_m = re.search(r'bitrate=\s*([\d.]+kbits/s)', decoded)
                if time_m:
                    bt_str = f" | Битрейт: {bitr_m.group(1)}" if bitr_m else ""
                    await send_log(websocket, f"Обработка: {time_m.group(1)}{bt_str}", to_terminal=False, msg_type="progress")

        await process.wait()
        end_time = asyncio.get_event_loop().time()
        
        if process.returncode != 0:
            err_details = get_last_error(log_file)
            await send_log(websocket, f"\n[!] ОШИБКА FFmpeg:\n{err_details}", msg_type="log")
            log_debug(f"[FFmpeg] FAILED: {title}")
            raise subprocess.CalledProcessError(process.returncode, " ".join(cmd))
            
        log_debug(f"[FFmpeg] Done: {title} (Заняло: {end_time - start_time:.2f}s)")

    except asyncio.CancelledError:
        if process:
            try: process.terminate(); await process.wait()
            except: pass
        log_debug(f"[FFmpeg] KILLED: {title}")
        raise

async def render_preview_chunk(websocket, timeline: List[VideoClip], request_time: float) -> Tuple[Optional[str], float]:
    tmp_dir = "/dev/shm" if os.path.exists("/dev/shm") else "."
    req_end = request_time + FRAGMENT_DURATION
    active_clips = [c for c in timeline if (c.global_start + c.duration) > request_time and c.global_start < req_end]
    if not active_clips: return None, request_time

    out_file = os.path.join(tmp_dir, f"tc_{str(uuid.uuid4())}.mkv")
    inputs, filters, v_pads, a_pads = [], [], [], []
    
    if HW_INFO["engine"] == "cuda":
        inputs.extend(['-init_hw_device', 'cuda=cu:0', '-filter_hw_device', 'cu'])
    elif HW_INFO["engine"] == "vaapi":
        inputs.extend(['-init_hw_device', f'vaapi=va:{HW_INFO["device"]}', '-filter_hw_device', 'va'])

    in_idx = 0
    for i, c in enumerate(active_clips):
        t_start = max(request_time, c.global_start)
        t_end = min(req_end, c.global_start + c.duration)
        needed_dur = t_end - t_start
        if needed_dur <= 0.05: continue

        offset_in_src = (t_start - c.global_start) + c.source_start
        seek = max(0, offset_in_src - SEEK_BUFFER)
        trim_st = offset_in_src - seek
        
        # Инпуты
        inputs.extend(['-ss', f"{seek:.4f}", '-i', c.source])
        base_idx = in_idx; in_idx += 1
        
        # 1. Base Scale
        v_base = f"[{base_idx}:v]{build_hw_scale_pad(HW_INFO['engine'], c.width, c.height, PREVIEW_WIDTH, PREVIEW_HEIGHT)}"
        filters.append(f"{v_base},trim=start={trim_st:.4f}:duration={needed_dur:.4f},setpts=PTS-STARTPTS[base_v{i}]")
        cur_v = f"[base_v{i}]"

        if c.has_overlay:
            pip_offset = (t_start - c.global_start) + c.overlay_source_start
            pip_seek = max(0, pip_offset - SEEK_BUFFER)
            pip_trim = pip_offset - pip_seek
            
            inputs.extend(['-ss', f"{pip_seek:.4f}", '-i', c.overlay_source])
            pip_idx = in_idx; in_idx += 1
            
            pw = int(PREVIEW_WIDTH * c.overlay_w); ph = int(pw * (9/16))
            px = int(PREVIEW_WIDTH * c.overlay_x); py = int(PREVIEW_HEIGHT * c.overlay_y)

            # 2. PiP Scale
            ov_info = await get_video_info(c.overlay_source)
            pip_scale = f"[{pip_idx}:v]{build_hw_scale_pip(HW_INFO['engine'], ov_info.get('width', 1920), ov_info.get('height', 1080), pw, ph)}"
            filters.append(f"{pip_scale},trim=start={pip_trim:.4f}:duration={needed_dur:.4f},setpts=PTS-STARTPTS[pip_v{i}]")

            # 3. Overlay
            if HW_INFO["overlay_supported"]:
                ovl_f = build_hw_overlay(HW_INFO["engine"], cur_v, f"[pip_v{i}]", px, py)
                filters.extend(ovl_f[:-1])
                filters.append(f"{ovl_f[-1]}[ovl_v{i}]")
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
    v_codec = "libx264"
    if HW_INFO["engine"] == "cuda":
        v_out_str += ",format=nv12,hwupload_cuda[v_out]"
        v_codec = "h264_nvenc"
    elif HW_INFO["engine"] == "vaapi":
        v_out_str += ",format=nv12,hwupload[v_out]"
        v_codec = "h264_vaapi"
    else:
        v_out_str += "[v_out]"
    filters.append(v_out_str)
    
    filters.append(f"{''.join(a_pads)}concat=n={len(a_pads)}:v=0:a=1[a_out]")

    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'level+time+verbose'] + inputs + \
          ['-filter_complex', ";".join(filters), '-map', '[v_out]', '-map', '[a_out]',
           '-c:v', v_codec]
    
    if v_codec == "h264_nvenc": cmd.extend(['-preset', 'p1', '-tune', 'll', '-delay', '0'])
    elif v_codec == "h264_vaapi": cmd.extend(['-preset', 'ultrafast'])
    else: cmd.extend(['-preset', 'ultrafast'])
    
    cmd.extend(['-r', '20', '-crf', '28', '-g', '15', '-c:a', 'aac', '-b:a', '128k', out_file, '-y'])
    
    await run_async_command(websocket, cmd, FFMPEG_PREVIEW_LOG, f"PREVIEW ({seconds_to_hms(request_time)})", is_preview=True)
    return out_file, request_time

class FinalRenderer:
    def __init__(self, websocket, timeline: List[VideoClip], output_path: str, intro_res: str):
        self.ws = websocket; self.timeline = timeline; self.output_path = output_path
        self.tmp_dir = "/dev/shm" if os.path.exists("/dev/shm") else "."
        self.out_dir = os.path.dirname(output_path)
        self.intro_res_mode = intro_res
        self.temp_files = []

    async def render(self):
        log_debug("[FINAL] Старт финального рендера")
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
                
                cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'verbose', '-stats']
                if HW_INFO["engine"] == "cuda":
                    cmd.extend(['-init_hw_device', 'cuda=cu:0', '-filter_hw_device', 'cu'])
                elif HW_INFO["engine"] == "vaapi":
                    cmd.extend(['-init_hw_device', f'vaapi=va:{HW_INFO["device"]}', '-filter_hw_device', 'va'])
                
                cmd.extend(['-ss', f"{c.source_start:.4f}", '-i', c.source])
                if c.has_overlay: cmd.extend(['-ss', f"{c.overlay_source_start:.4f}", '-i', c.overlay_source])
                cmd.extend(['-t', f"{c.duration:.4f}"])
                
                vf = [f"[0:v]{build_hw_scale_pad(HW_INFO['engine'], c.width, c.height, target_w, target_h)},fps={master['fps']}[base_v]"]
                cur_v = "[base_v]"
                
                if c.has_overlay:
                    ov_info = await get_video_info(c.overlay_source)
                    pw = int(target_w * c.overlay_w); ph = int(pw * (9/16)); pw -= pw % 2; ph -= ph % 2
                    px = int(target_w * c.overlay_x); py = int(target_h * c.overlay_y)
                    vf.append(f"[1:v]{build_hw_scale_pip(HW_INFO['engine'], ov_info.get('width',1920), ov_info.get('height',1080), pw, ph)},fps={master['fps']}[pip_v]")
                    
                    if HW_INFO["overlay_supported"]:
                        ovl_f = build_hw_overlay(HW_INFO["engine"], cur_v, "[pip_v]", px, py)
                        vf.extend(ovl_f[:-1])
                        vf.append(f"{ovl_f[-1]}[ovl_v]")
                    else:
                        vf.append(f"{cur_v}[pip_v]overlay=x={px}:y={py}:eof_action=pass[ovl_v]")
                    cur_v = "[ovl_v]"

                if c.fade_in: vf.append(f"{cur_v}fade=in:st=0:d={FADE_DURATION}[fv_in]"); cur_v = "[fv_in]"
                if c.fade_out: vf.append(f"{cur_v}fade=out:st={c.duration-FADE_DURATION:.4f}:d={FADE_DURATION}[fv_out]"); cur_v = "[fv_out]"
                
                v_codec = VIDEO_ENCODER
                if HW_INFO["engine"] == "cuda":
                    vf.append(f"{cur_v}format=nv12,hwupload_cuda[out_v]")
                    cur_v = "[out_v]"
                    v_codec = "h264_nvenc"
                elif HW_INFO["engine"] == "vaapi":
                    vf.append(f"{cur_v}format=nv12,hwupload[out_v]")
                    cur_v = "[out_v]"
                    v_codec = "h264_vaapi"
                
                af = [f"[0:a]volume={c.volume}[base_a]"]
                cur_a = "[base_a]"
                if c.has_overlay and c.overlay_audio:
                    af.append(f"[1:a]volume=1.0[pip_a]")
                    af.append(f"{cur_a}[pip_a]amix=inputs=2:duration=first:dropout_transition=2[mix_a]"); cur_a = "[mix_a]"
                if c.fade_in: af.append(f"{cur_a}afade=t=in:st=0:d={FADE_DURATION}[fa_in]"); cur_a = "[fa_in]"
                if c.fade_out: af.append(f"{cur_a}afade=t=out:st={c.duration-FADE_DURATION:.4f}:d={FADE_DURATION}[fa_out]"); cur_a = "[fa_out]"

                cmd.extend(['-filter_complex', f"{';'.join(vf)};{';'.join(af)}", '-map', cur_v, '-map', cur_a, '-c:v', v_codec])
                if v_codec == "h264_nvenc": cmd.extend(['-preset', 'p6'])
                
                cmd.extend(['-crf', '18', '-c:a', FINAL_AUDIO_CODEC, out, '-y'])
                
                await run_async_command(self.ws, cmd, FFMPEG_FINAL_LOG, f"Адаптация: {c.name}")
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
        
        final_cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'verbose'] + cmd_inputs + \
                    ['-filter_complex', ";".join(a_filters), '-map', '0:v', '-map', '[aout]', '-c:v', 'copy', '-c:a', FINAL_AUDIO_CODEC, self.output_path, '-y']
        
        await run_async_command(self.ws, final_cmd, FFMPEG_FINAL_LOG, "Финальная склейка")
        for f in self.temp_files: 
            if os.path.exists(f): 
                try: os.remove(f)
                except: pass