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
            '-init_hw_device', f'vaapi=va:{HW_INFO["device"]}', '-filter_hw_device', 'va',
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
                '-init_hw_device', f'vaapi=va:{HW_INFO["device"]}', '-filter_hw_device', 'va', 
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

def calc_ar_dims(src_w: int, src_h: int, max_w: int, max_h: int) -> Tuple[int, int]:
    if src_w == 0 or src_h == 0: return max_w, max_h
    ratio = min(max_w / src_w, max_h / src_h)
    tw, th = int(src_w * ratio), int(src_h * ratio)
    return tw - (tw % 2), th - (th % 2)

def seconds_to_hms(seconds: float) -> str:
    h = int(seconds // 3600); m = int((seconds % 3600) // 60); s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:05.2f}"

def get_last_error(log_file: str, lines: int = 4) -> str:
    try:
        with open(log_file, "r", encoding="utf-8") as f:
            content = f.readlines()
            errors = [line.strip() for line in content if "frame=" not in line and "fps=" not in line and line.strip()]
            return "\n".join(errors[-lines:])
    except: return "Не удалось прочитать подробности из лога."

async def send_log(websocket, message: str, to_terminal: bool = True, msg_type: str = "log"):
    try: 
        await websocket.send(json.dumps({ "action": "log", "message": message, "type": msg_type }))
        if to_terminal: log_debug(f"[UI-LOG] {message}", to_console=True)
    except: pass

async def run_async_command(websocket, command: List[str], log_file: str, title: str = "", is_preview: bool = False, total_dur: float = 0):
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
                fps_m = re.search(r'fps=\s*([\d.]+)', decoded)
                
                if time_m:
                    time_str = time_m.group(1)
                    fps_str = f" | Скорость: {fps_m.group(1)} fps" if fps_m else ""
                    dur_str = f" / {seconds_to_hms(total_dur)}" if total_dur > 0 else ""
                    
                    await send_log(websocket, f"Обработка: {time_str}{dur_str}{fps_str}", to_terminal=False, msg_type="progress")

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
    fps = PREVIEW_FPS
    tw_max, th_max = PREVIEW_WIDTH, PREVIEW_HEIGHT

    for i, c in enumerate(active_clips):
        t_start = max(request_time, c.global_start)
        t_end = min(req_end, c.global_start + c.duration)
        needed_dur = t_end - t_start
        if needed_dur <= 0.05: continue

        offset_in_src = (t_start - c.global_start) + c.source_start
        
        inputs.extend(['-ss', f"{offset_in_src:.4f}", '-t', f"{(needed_dur + 0.5):.4f}", '-i', c.source])
        base_idx = in_idx; in_idx += 1
        
        filters.append(f"[{base_idx}:v]trim=start=0:duration={needed_dur:.4f},setpts=PTS-STARTPTS[base_t{i}]")
        cur_v = f"[base_t{i}]"
        is_hw = False

        tw, th = calc_ar_dims(c.width, c.height, tw_max, th_max)

        if HW_INFO["engine"] != "cpu" and HW_INFO["overlay_supported"]:
            up = "hwupload_cuda" if HW_INFO["engine"] == "cuda" else "hwupload"
            sc = f"scale_cuda={tw}:{th}:format=nv12" if HW_INFO["engine"] == "cuda" else f"scale_vaapi=w={tw}:h={th}"

            if tw == tw_max and th == th_max:
                filters.append(f"{cur_v}format=nv12,{up},{sc}[padded_hw_{i}]")
                cur_v = f"[padded_hw_{i}]"
            else:
                x_off, y_off = (tw_max - tw) // 2, (th_max - th) // 2
                ov = f"overlay_cuda=x={x_off}:y={y_off}:eof_action=pass" if HW_INFO["engine"] == "cuda" else f"overlay_vaapi=x={x_off}:y={y_off}:eof_action=pass"
                filters.append(f"color=black:s={tw_max}x{th_max}:d={needed_dur:.4f}:r={fps},format=nv12,{up}[bg_{i}]")
                filters.append(f"{cur_v}format=nv12,{up},{sc}[vid_hw_{i}]")
                filters.append(f"[bg_{i}][vid_hw_{i}]{ov}[padded_hw_{i}]")
                cur_v = f"[padded_hw_{i}]"
            
            is_hw = True
        else:
            if tw == tw_max and th == th_max:
                filters.append(f"{cur_v}scale={tw_max}:{th_max},setsar=1[padded_sw_{i}]")
            else:
                filters.append(f"{cur_v}scale={tw_max}:{th_max}:force_original_aspect_ratio=decrease,pad={tw_max}:{th_max}:(ow-iw)/2:(oh-ih)/2,setsar=1[padded_sw_{i}]")
            cur_v = f"[padded_sw_{i}]"

        if c.has_overlay:
            pip_offset = (t_start - c.global_start) + c.overlay_source_start
            
            inputs.extend(['-ss', f"{pip_offset:.4f}", '-t', f"{(needed_dur + 0.5):.4f}", '-i', c.overlay_source])
            pip_idx = in_idx; in_idx += 1
            
            filters.append(f"[{pip_idx}:v]trim=start=0:duration={needed_dur:.4f},setpts=PTS-STARTPTS[pip_t{i}]")
            
            pw = int(tw_max * c.overlay_w); ph = int(pw * (9/16))
            pw -= pw % 2; ph -= ph % 2
            px = int(tw_max * c.overlay_x); py = int(th_max * c.overlay_y)

            if is_hw:
                ov_info = await get_video_info(c.overlay_source)
                tw_pip, th_pip = calc_ar_dims(ov_info.get('width', 1920), ov_info.get('height', 1080), pw, ph)
                
                sc_pip = f"scale_cuda={tw_pip}:{th_pip}:format=nv12" if HW_INFO["engine"] == "cuda" else f"scale_vaapi=w={tw_pip}:h={th_pip}"
                ov_pip = f"overlay_cuda=x={px}:y={py}:eof_action=pass" if HW_INFO["engine"] == "cuda" else f"overlay_vaapi=x={px}:y={py}:eof_action=pass"
                
                filters.append(f"[pip_t{i}]format=nv12,{up},{sc_pip}[pip_hw_{i}]")
                filters.append(f"{cur_v}[pip_hw_{i}]{ov_pip}[ovl_hw_{i}]")
                cur_v = f"[ovl_hw_{i}]"
            else:
                filters.append(f"[pip_t{i}]scale={pw}:{ph},setsar=1[pip_sw_{i}]")
                filters.append(f"{cur_v}[pip_sw_{i}]overlay=x={px}:y={py}:eof_action=pass[ovl_sw_{i}]")
                cur_v = f"[ovl_sw_{i}]"

        if c.is_fade:
            needs_fade = False
            if c.fade_in and (t_start - c.global_start) < FADE_DURATION: needs_fade = True
            if c.fade_out and (c.duration - (t_start - c.global_start + needed_dur)) < FADE_DURATION: needs_fade = True
            
            if needs_fade:
                if is_hw:
                    filters.append(f"{cur_v}hwdownload,format=nv12[fade_down_{i}]")
                    cur_v = f"[fade_down_{i}]"
                    is_hw = False
                    
                if c.fade_in and (t_start - c.global_start) < FADE_DURATION:
                    filters.append(f"{cur_v}fade=in:st=0:d={FADE_DURATION}[fv_in{i}]"); cur_v = f"[fv_in{i}]"
                if c.fade_out and (c.duration - (t_start - c.global_start + needed_dur)) < FADE_DURATION:
                    filters.append(f"{cur_v}fade=out:st={max(0, needed_dur-FADE_DURATION):.4f}:d={FADE_DURATION}[fv_out{i}]"); cur_v = f"[fv_out{i}]"
                
        v_pads.append((cur_v, is_hw))
        
        filters.append(f"[{base_idx}:a]atrim=start=0:duration={needed_dur:.4f},asetpts=PTS-STARTPTS,volume={c.volume}[base_a{i}]")
        cur_a = f"[base_a{i}]"
        
        if c.has_overlay and c.overlay_audio:
            filters.append(f"[{pip_idx}:a]atrim=start=0:duration={needed_dur:.4f},asetpts=PTS-STARTPTS,volume=1.0[pip_a{i}]")
            filters.append(f"{cur_a}[pip_a{i}]amix=inputs=2:duration=first:dropout_transition=2[mix_a{i}]"); cur_a = f"[mix_a{i}]"
        a_pads.append(cur_a)

    v_codec = "libx264"
    if len(v_pads) == 1:
        final_v, is_hw = v_pads[0]
        if is_hw:
            v_out_str = final_v
            v_codec = "h264_nvenc" if HW_INFO["engine"] == "cuda" else "h264_vaapi"
        else:
            if HW_INFO["engine"] == "cuda":
                filters.append(f"{final_v}format=nv12,hwupload_cuda[v_out]")
                v_out_str, v_codec = "[v_out]", "h264_nvenc"
            elif HW_INFO["engine"] == "vaapi":
                filters.append(f"{final_v}format=nv12,hwupload[v_out]")
                v_out_str, v_codec = "[v_out]", "h264_vaapi"
            else:
                v_out_str = final_v
    else:
        concat_inputs = ""
        for i, (v, is_hw) in enumerate(v_pads):
            if is_hw:
                filters.append(f"{v}hwdownload,format=nv12[down_{i}]")
                concat_inputs += f"[down_{i}]"
            else:
                concat_inputs += v
        
        filters.append(f"{concat_inputs}concat=n={len(v_pads)}:v=1:a=0[v_concat]")
        
        if HW_INFO["engine"] == "cuda":
            filters.append(f"[v_concat]format=nv12,hwupload_cuda[v_out]")
            v_out_str, v_codec = "[v_out]", "h264_nvenc"
        elif HW_INFO["engine"] == "vaapi":
            filters.append(f"[v_concat]format=nv12,hwupload[v_out]")
            v_out_str, v_codec = "[v_out]", "h264_vaapi"
        else:
            v_out_str = "[v_concat]"
    
    filters.append(f"{''.join(a_pads)}concat=n={len(a_pads)}:v=0:a=1[a_out]")

    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'level+time+verbose'] + inputs + \
          ['-filter_complex', ";".join(filters), '-map', v_out_str, '-map', '[a_out]', '-c:v', v_codec]
    
    if v_codec == "h264_nvenc": cmd.extend(['-preset', 'p1', '-tune', 'll', '-delay', '0'])
    elif v_codec == "h264_vaapi": cmd.extend(['-preset', 'ultrafast'])
    else: cmd.extend(['-preset', 'ultrafast'])
    
    cmd.extend(['-r', str(PREVIEW_FPS), '-crf', '28', '-g', '15', '-c:a', FINAL_AUDIO_CODEC, '-b:a', '128k', out_file, '-y'])
    
    # Для превью общая длительность - это FRAGMENT_DURATION
    await run_async_command(websocket, cmd, FFMPEG_PREVIEW_LOG, f"PREVIEW ({seconds_to_hms(request_time)})", is_preview=True, total_dur=FRAGMENT_DURATION)
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
        
        # Общая длительность всего видео для финальной склейки
        total_final_duration = sum(c.duration for c in self.timeline)
        
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
                
                filters = []
                tw, th = calc_ar_dims(c.width, c.height, target_w, target_h)
                
                if HW_INFO["engine"] != "cpu" and HW_INFO["overlay_supported"]:
                    up = "hwupload_cuda" if HW_INFO["engine"] == "cuda" else "hwupload"
                    sc = f"scale_cuda={tw}:{th}:format=nv12" if HW_INFO["engine"] == "cuda" else f"scale_vaapi=w={tw}:h={th}"
                    
                    if tw == target_w and th == target_h:
                        filters.append(f"[0:v]format=nv12,{up},{sc}[padded_hw]")
                        cur_v = "[padded_hw]"
                    else:
                        x_off, y_off = (target_w - tw) // 2, (target_h - th) // 2
                        ov = f"overlay_cuda=x={x_off}:y={y_off}:eof_action=pass" if HW_INFO["engine"] == "cuda" else f"overlay_vaapi=x={x_off}:y={y_off}:eof_action=pass"
                        filters.append(f"color=black:s={target_w}x{target_h}:d={c.duration:.4f}:r={master['fps']},format=nv12,{up}[bg]")
                        filters.append(f"[0:v]format=nv12,{up},{sc}[vid_hw]")
                        filters.append(f"[bg][vid_hw]{ov}[padded_hw]")
                        cur_v = "[padded_hw]"
                        
                    is_hw = True
                else:
                    if tw == target_w and th == target_h:
                        filters.append(f"[0:v]scale={target_w}:{target_h},setsar=1,fps={master['fps']}[padded_sw]")
                    else:
                        filters.append(f"[0:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={master['fps']}[padded_sw]")
                    cur_v = "[padded_sw]"
                    is_hw = False
                
                if c.has_overlay:
                    ov_info = await get_video_info(c.overlay_source)
                    pw = int(target_w * c.overlay_w); ph = int(pw * (9/16)); pw -= pw % 2; ph -= ph % 2
                    px = int(target_w * c.overlay_x); py = int(target_h * c.overlay_y)
                    
                    if is_hw:
                        tw_pip, th_pip = calc_ar_dims(ov_info.get('width', 1920), ov_info.get('height', 1080), pw, ph)
                        sc_pip = f"scale_cuda={tw_pip}:{th_pip}:format=nv12" if HW_INFO["engine"] == "cuda" else f"scale_vaapi=w={tw_pip}:h={th_pip}"
                        ov_pip = f"overlay_cuda=x={px}:y={py}:eof_action=pass" if HW_INFO["engine"] == "cuda" else f"overlay_vaapi=x={px}:y={py}:eof_action=pass"
                        
                        filters.append(f"[1:v]format=nv12,{up},{sc_pip}[pip_hw]")
                        filters.append(f"{cur_v}[pip_hw]{ov_pip}[ovl_hw]")
                        cur_v = "[ovl_hw]"
                    else:
                        filters.append(f"[1:v]scale={pw}:{ph},setsar=1[pip_sw]")
                        filters.append(f"{cur_v}[pip_sw]overlay=x={px}:y={py}:eof_action=pass[ovl_sw]")
                        cur_v = "[ovl_sw]"

                if c.is_fade:
                    if is_hw:
                        filters.append(f"{cur_v}hwdownload,format=nv12[fade_down]")
                        cur_v = "[fade_down]"
                        is_hw = False
                    if c.fade_in: filters.append(f"{cur_v}fade=in:st=0:d={FADE_DURATION}[fv_in]"); cur_v = "[fv_in]"
                    if c.fade_out: filters.append(f"{cur_v}fade=out:st={c.duration-FADE_DURATION:.4f}:d={FADE_DURATION}[fv_out]"); cur_v = "[fv_out]"
                
                v_codec = VIDEO_ENCODER
                v_out_str = cur_v
                if is_hw:
                    v_codec = "h264_nvenc" if HW_INFO["engine"] == "cuda" else "h264_vaapi"
                else:
                    if HW_INFO["engine"] == "cuda":
                        filters.append(f"{cur_v}format=nv12,hwupload_cuda[out_v]")
                        v_out_str, v_codec = "[out_v]", "h264_nvenc"
                    elif HW_INFO["engine"] == "vaapi":
                        filters.append(f"{cur_v}format=nv12,hwupload[out_v]")
                        v_out_str, v_codec = "[out_v]", "h264_vaapi"

                
                af = [f"[0:a]volume={c.volume}[base_a]"]
                cur_a = "[base_a]"
                if c.has_overlay and c.overlay_audio:
                    af.append(f"[1:a]volume=1.0[pip_a]")
                    af.append(f"{cur_a}[pip_a]amix=inputs=2:duration=first:dropout_transition=2[mix_a]"); cur_a = "[mix_a]"
                if c.fade_in: af.append(f"{cur_a}afade=t=in:st=0:d={FADE_DURATION}[fa_in]"); cur_a = "[fa_in]"
                if c.fade_out: af.append(f"{cur_a}afade=t=out:st={c.duration-FADE_DURATION:.4f}:d={FADE_DURATION}[fa_out]"); cur_a = "[fa_out]"

                cmd.extend(['-filter_complex', f"{';'.join(filters)};{';'.join(af)}", '-map', v_out_str, '-map', cur_a, '-c:v', v_codec])
                if v_codec == "h264_nvenc": cmd.extend(['-preset', 'p6'])
                
                cmd.extend(['-crf', '18', '-c:a', FINAL_AUDIO_CODEC, out, '-y'])
                
                # Для адаптации сегмента общая длительность - это c.duration
                await run_async_command(self.ws, cmd, FFMPEG_FINAL_LOG, f"Адаптация: {c.name}", total_dur=c.duration)
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
        
        # Для финальной склейки общая длительность - это сумма всех клипов
        await run_async_command(self.ws, final_cmd, FFMPEG_FINAL_LOG, "Финальная склейка", total_dur=total_final_duration)
        for f in self.temp_files: 
            if os.path.exists(f): 
                try: os.remove(f)
                except: pass
