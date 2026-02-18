## @file timeline.py
# @brief Модуль анализа видео и построения таймлайна (N-сегментов).

import os
import uuid
import asyncio
import subprocess
from typing import List, Dict, Any, Optional
from config import *

async def get_video_info(file_path: str) -> Dict[str, Any]:
    if not os.path.exists(file_path): return {}
    try:
        cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', 
               '-show_entries', 'format=duration:stream=width,height,r_frame_rate,sample_aspect_ratio', 
               '-of', 'json', file_path]
        process = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, _ = await process.communicate()
        data = json.loads(out)
        f, s = data.get('format', {}), data.get('streams', [{}])[0]
        fps_raw = s.get('r_frame_rate', '30/1')
        num, den = map(int, fps_raw.split('/'))
        return {'duration': float(f.get('duration', 0)), 'width': int(s.get('width', 0)), 'height': int(s.get('height', 0)), 'fps': num/den, 'sar': s.get('sample_aspect_ratio', '1:1')}
    except: return {}

async def analyze_keyframes(file_path: str, cache_dir: str) -> List[float]:
    filename = os.path.basename(file_path)
    cache_file = os.path.join(cache_dir, f"{filename}.keyframes.txt")
    if not os.path.exists(cache_file):
        log_debug(f"[ANALYSIS] Generating I-Frames for {filename}")
        cmd = ['ffprobe','-v','error','-select_streams','v:0','-show_entries','packet=pts_time,flags','-of','csv=p=0', file_path]
        process = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, _ = await process.communicate()
        keyframes = [float(l.split(',')[0]) for l in out.decode().strip().split('\n') if ',K' in l]
        with open(cache_file, 'w') as f: f.write('\n'.join(map(str, keyframes)))
        return keyframes
    with open(cache_file, 'r') as f: return [float(x) for x in f.read().split('\n') if x]

def hms_to_seconds(time_str: str) -> float:
    if not time_str: return 0.0
    parts = str(time_str).split(':')
    s = 0.0
    try:
        if len(parts) == 3: s = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2: s = int(parts[0]) * 60 + float(parts[1])
        elif len(parts) == 1 and time_str: s = float(time_str)
    except (ValueError, TypeError): s = 0.0
    return float(s)

class VideoClip:
    def __init__(self, name: str, source: str, duration: float, source_start: float = 0.0, 
                 is_fade: bool = False, volume: float = 1.0, audio_source: Optional[str] = None,
                 color: str = "blue"):
        self.uuid = str(uuid.uuid4())[:8]
        self.name = name
        self.source = source
        self.audio_source = audio_source if audio_source else source
        self.duration = float(duration)
        self.source_start = float(source_start)
        self.is_fade = is_fade
        self.volume = volume
        self.color = color 
        self.video_filters: List[str] = []
        self.audio_filters: List[str] = []
        self.global_start = 0.0 

    def to_dict(self):
        return {
            "name": self.name,
            "duration": self.duration,
            "global_start": self.global_start,
            "color": self.color,
            "is_fade": self.is_fade
        }
        
    def __repr__(self):
        return f"Clip({self.name}, dur={self.duration:.2f}, g_start={self.global_start:.2f})"

class TimelineBuilder:
    def __init__(self, params: Dict[str, Any], tmp_dir: str):
        self.params = params
        self.tmp_dir = tmp_dir

    async def build(self) -> List[VideoClip]:
        log_debug("[BUILDER] Строим таймлайн")
        timeline = []
        
        # 1. Intro Logic
        intro_res = self.params.get('intro_resolution', '2k')
        # Пытаемся найти интро по приоритету:
        # 1. Явно заданный файл в UI
        # 2. Файл с разрешением в HOME_DIR
        # 3. Файл с разрешением в DEFAULT_INTRO_DIR
        
        intro_path = self.params.get('intro_file')
        if not intro_path or not os.path.exists(intro_path):
             # Авто-поиск
             candidates = [
                 os.path.join(HOME_DIR, f"{INTRO_BASE_NAME}_{intro_res}.mkv"),
                 os.path.join(DEFAULT_INTRO_DIR, f"{INTRO_BASE_NAME}_{intro_res}.mkv")
             ]
             for c in candidates:
                 if os.path.exists(c):
                     intro_path = c
                     break
        
        if intro_path and os.path.exists(intro_path):
            info = await get_video_info(intro_path)
            timeline.append(VideoClip("Intro", intro_path, info.get('duration', 0), color="yellow"))
        else:
            log_debug("[BUILDER] Интро не найдено или не выбрано")

        # 2. Segments Parsing (N-segments support)
        vol = float(self.params.get('volume', 1.0))
        
        raw_segments = self.params.get('segments_list', [])
        
        # Обратная совместимость для старого формата (video1, video2...)
        if not raw_segments and self.params.get('video1'):
            log_debug("[BUILDER] Используется легаси формат параметров")
            raw_segments.append({'video': self.params.get('video1'), 'audio': self.params.get('audio1'), 'start': self.params.get('start1'), 'end': self.params.get('end1')})
            # Проверяем video2
            if not (self.params.get('is_single_segment') and self.params.get('mode') == 'single'):
                 v2 = self.params.get('video2') or (self.params.get('video1') if self.params.get('mode') == 'single' else None)
                 if v2:
                     a2 = self.params.get('audio2') or (self.params.get('audio1') if self.params.get('mode') == 'single' else None)
                     raw_segments.append({'video': v2, 'audio': a2, 'start': self.params.get('start2'), 'end': self.params.get('end2')})

        for i, seg in enumerate(raw_segments):
            v_path = seg.get('video')
            a_path = seg.get('audio') or v_path # Если аудио не указано, берем из видео
            
            if not v_path or not os.path.exists(v_path): continue
            
            t_start = hms_to_seconds(seg.get('start'))
            t_end = hms_to_seconds(seg.get('end'))
            
            info = await get_video_info(v_path)
            # Если конец не указан или 0, берем до конца файла
            if t_end <= t_start: t_end = info.get('duration', 0)
            
            dur = t_end - t_start
            if dur <= 0: continue

            # Если слишком коротко для фейдов
            if dur < FADE_DURATION * 2:
                timeline.append(VideoClip(f"Seg{i+1}", v_path, dur, t_start, volume=vol, audio_source=a_path, color="blue"))
                continue

            keyframes = await analyze_keyframes(v_path, self.tmp_dir)
            
            # Поиск точек разреза для Smart Copy
            split_start = next((t for t in keyframes if t > t_start + FADE_DURATION), None)
            split_end = next((t for t in reversed(keyframes) if t < t_end - FADE_DURATION), None)

            # Если не нашли подходящих I-кадров внутри, рендерим весь кусок с перекодированием
            if not split_start or not split_end or split_end <= split_start:
                c = VideoClip(f"Seg{i+1}_Full", v_path, dur, t_start, is_fade=True, volume=vol, audio_source=a_path, color="lightblue")
                c.video_filters = [f"fade=in:st=0:d={FADE_DURATION}", f"fade=out:st={dur-FADE_DURATION}:d={FADE_DURATION}"]
                c.audio_filters = [f"afade=t=in:st=0:d={FADE_DURATION}", f"afade=t=out:st={dur-FADE_DURATION}:d={FADE_DURATION}"]
                timeline.append(c)
            else:
                # 1. Fade In (Transcode)
                dur_in = split_start - t_start
                c_in = VideoClip(f"Seg{i+1}_In", v_path, dur_in, t_start, is_fade=True, volume=vol, audio_source=a_path, color="lightblue")
                c_in.video_filters, c_in.audio_filters = [f"fade=in:st=0:d={FADE_DURATION}"], [f"afade=t=in:st=0:d={FADE_DURATION}"]
                timeline.append(c_in)
                
                # 2. Body (Stream Copy Candidate)
                dur_body = split_end - split_start
                timeline.append(VideoClip(f"Seg{i+1}_Body", v_path, dur_body, split_start, volume=vol, audio_source=a_path, color="blue"))
                
                # 3. Fade Out (Transcode)
                dur_out = t_end - split_end
                # Fade out relative start inside this small clip
                fout_st = dur_out - FADE_DURATION
                c_out = VideoClip(f"Seg{i+1}_Out", v_path, dur_out, split_end, is_fade=True, volume=vol, audio_source=a_path, color="lightblue")
                c_out.video_filters = [f"fade=out:st={fout_st:.4f}:d={FADE_DURATION}"]
                c_out.audio_filters = [f"afade=t=out:st={fout_st:.4f}:d={FADE_DURATION}"]
                timeline.append(c_out)

        # Пересчет глобального времени
        curr = 0.0
        for c in timeline:
            c.global_start = curr
            curr += c.duration
        
        log_debug(f"[BUILDER] Готово. Клипов: {len(timeline)}")
        return timeline