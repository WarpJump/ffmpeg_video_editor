
## @file timeline.py
# @brief Модуль анализа видео и построения таймлайна (IR).

import os
import uuid
import asyncio
import subprocess
import json
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
        self.global_start = 0.0 
        
        self.fade_in = False
        self.fade_out = False
        
        # PiP Data
        self.has_overlay = False
        self.overlay_source = ""
        self.overlay_source_start = 0.0
        self.overlay_x = 0.7  # % от ширины
        self.overlay_y = 0.7  # % от высоты
        self.overlay_w = 0.25 # % от ширины (scale)
        self.overlay_audio = False
        self.segment_id = None # Для связи UI блоков

    def to_dict(self):
        return {
            "name": self.name,
            "duration": self.duration,
            "global_start": self.global_start,
            "color": self.color,
            "is_fade": self.is_fade,
            "has_overlay": self.has_overlay,
            "segment_id": self.segment_id,
            "overlay_coords": { "x": self.overlay_x, "y": self.overlay_y, "w": self.overlay_w } if self.has_overlay else None
        }

class TimelineBuilder:
    def __init__(self, params: Dict[str, Any], tmp_dir: str):
        self.params = params
        self.tmp_dir = tmp_dir

    async def build(self) -> List[VideoClip]:
        timeline = []
        
        # 1. Intro Logic
        intro_path = self.params.get('intro_file')
        if not intro_path or not os.path.exists(intro_path):
             intro_res = self.params.get('intro_resolution', '2k')
             candidates = [os.path.join(HOME_DIR, f"{INTRO_BASE_NAME}_{intro_res}.mkv"), os.path.join(DEFAULT_INTRO_DIR, f"{INTRO_BASE_NAME}_{intro_res}.mkv")]
             for c in candidates:
                 if os.path.exists(c): intro_path = c; break
        
        if intro_path and os.path.exists(intro_path):
            info = await get_video_info(intro_path)
            timeline.append(VideoClip("Intro", intro_path, info.get('duration', 0), color="yellow"))

        # 2. Segments Parsing
        vol = float(self.params.get('volume', 1.0))
        raw_segments = self.params.get('segments_list', [])

        for i, seg in enumerate(raw_segments):
            v_path = seg.get('video')
            a_path = seg.get('audio') or v_path
            if not v_path or not os.path.exists(v_path): continue
            
            t_start = hms_to_seconds(seg.get('start'))
            t_end = hms_to_seconds(seg.get('end'))
            info = await get_video_info(v_path)
            if t_end <= t_start: t_end = info.get('duration', 0)
            
            dur = t_end - t_start
            if dur <= 0: continue
            
            pip_data = seg.get('overlay', {})
            has_pip = bool(pip_data and pip_data.get('video') and os.path.exists(pip_data.get('video')))

            if has_pip or dur < FADE_DURATION * 2:
                c = VideoClip(f"Seg{i+1}_Full", v_path, dur, t_start, is_fade=True, volume=vol, audio_source=a_path, color="lightblue")
                c.segment_id = seg.get('id')
                if dur >= FADE_DURATION * 2: c.fade_in, c.fade_out = True, True
                if has_pip:
                    c.has_overlay = True
                    c.overlay_source = pip_data['video']
                    c.overlay_source_start = hms_to_seconds(pip_data.get('start'))
                    c.overlay_x = float(pip_data.get('x', 0.7))
                    c.overlay_y = float(pip_data.get('y', 0.7))
                    c.overlay_w = float(pip_data.get('w', 0.25))
                    c.overlay_audio = bool(pip_data.get('mix_audio', False))
                    c.color = "pink"
                timeline.append(c)
                continue

            keyframes = await analyze_keyframes(v_path, self.tmp_dir)
            split_start = next((t for t in keyframes if t > t_start + FADE_DURATION), None)
            split_end = next((t for t in reversed(keyframes) if t < t_end - FADE_DURATION), None)

            if not split_start or not split_end or split_end <= split_start:
                c = VideoClip(f"Seg{i+1}_Full", v_path, dur, t_start, is_fade=True, volume=vol, audio_source=a_path, color="lightblue")
                c.segment_id = seg.get('id'); c.fade_in, c.fade_out = True, True
                timeline.append(c)
            else:
                c_in = VideoClip(f"Seg{i+1}_In", v_path, split_start - t_start, t_start, is_fade=True, volume=vol, audio_source=a_path, color="lightblue")
                c_in.segment_id = seg.get('id'); c_in.fade_in = True
                timeline.append(c_in)
                
                c_body = VideoClip(f"Seg{i+1}_Body", v_path, split_end - split_start, split_start, volume=vol, audio_source=a_path, color="blue")
                c_body.segment_id = seg.get('id'); timeline.append(c_body)
                
                c_out = VideoClip(f"Seg{i+1}_Out", v_path, t_end - split_end, split_end, is_fade=True, volume=vol, audio_source=a_path, color="lightblue")
                c_out.segment_id = seg.get('id'); c_out.fade_out = True
                timeline.append(c_out)

        curr = 0.0
        for c in timeline:
            c.global_start = curr
            curr += c.duration
        
        return timeline