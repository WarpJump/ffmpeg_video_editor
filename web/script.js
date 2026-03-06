let ws;
let segments = [];
let globalSettings = { introPath: '', outputDir: '', volume: 1.5, useRam: true };
let currentBrowseId = null;
let activePipSegmentId = null;
let dragDebounceTimer = null;

// --- PLAYER DEBUG & STATE ---
let seekRequestId = 0;
let preloadRequestId = 0;
let debug_expectedTime = null;
let debug_currentReason = "";

function logPlayer(action, reason, details = {}) {
    const time = new Date().toISOString().split('T')[1].slice(0, -1);
    let color = "#00d8ff";
    if (action === "REQUEST") color = "#f39c12";
    if (action === "PLAY") color = "#2ecc71";
    if (action === "MISMATCH") color = "#e74c3c";
    if (action === "CACHE_CLEAR") color = "#9b59b6";
    console.log(`%c[Player ${time}] [${action}] %c${reason}`, `color: ${color}; font-weight: bold;`, `color: white; font-weight: normal;`, details);
}

function requestPreviewFragment(time, reason, isPreload = false) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;

    let reqId;
    if (isPreload) {
        preloadRequestId++;
        reqId = preloadRequestId;
        logPlayer("REQUEST (Preload)", reason, { time, reqId });
    } else {
        seekRequestId++;
        reqId = seekRequestId;
        // КРИТИЧНО: При ручной перемотке мгновенно инвалидируем любые летящие предзагрузки
        preloadRequestId++;
        streamer.nextChunkRequested = false;

        debug_expectedTime = time;
        debug_currentReason = reason;
        logPlayer("REQUEST", reason, { time, reqId });
    }

    ws.send(JSON.stringify({
        action: 'generate_preview_fragment',
        start_time: time,
        exact_time: time,
        is_preload: isPreload,
        request_id: reqId,
        params: gatherParams()
    }));
}
const streamer = {
    totalDuration: 0,
    isPlaying: false,
    currentPlayer: null,
    nextPlayer: null,
    currentChunkStartTime: 0,
    nextChunkStartTime: -1, // ДОБАВЛЕНО: хранилище времени предзагрузки
    fragmentDuration: 10.0, // Добавлено строгое значение по умолчанию
    isLoading: false,
    nextChunkRequested: false, // Флаг "Запрос отправлен, но ответ еще не обработан"
    clips: [],

    init: function () {
        this.currentPlayer = document.getElementById('videoPlayerA');
        this.nextPlayer = document.getElementById('videoPlayerB');

        // Инициализация скрытых полей времени
        this.currentPlayer._chunkStart = -1;
        this.nextPlayer._chunkStart = -1;

        this.currentPlayer.onended = () => this.swap();
        this.nextPlayer.onended = () => this.swap();
        this.currentPlayer.ontimeupdate = () => this.onTimeUpdate();
        document.getElementById('v-play-btn').onclick = () => this.togglePlay();
    },

    togglePlay: function () {
        this.isPlaying = !this.isPlaying;
        document.getElementById('v-play-btn').textContent = this.isPlaying ? '⏸' : '▶';
        if (this.isPlaying) {
            if (this.currentPlayer.readyState >= 2) this.currentPlayer.play();
            else this.seek(0, "Нажатие Play (с нуля)");
        } else this.currentPlayer.pause();
    },

    seek: function (time, reason = "Перемотка") {
        this.isPlaying = true;
        this.isLoading = true;
        document.getElementById('v-play-btn').textContent = '⏸';
        document.getElementById('loading-overlay').style.display = 'flex';
        this.currentPlayer.pause();
        this.nextPlayer.pause();

        // Сброс буферов
        this.nextChunkRequested = false;
        this.nextChunkStartTime = -1;
        this.nextPlayer.removeAttribute('src'); // Очистка следующего плеера
        this.nextPlayer._chunkStart = -1;
        this.nextPlayer.load();

        requestPreviewFragment(time, reason, false);
    },
    onChunkReady: function (data) {
        // Проверяем, не принадлежит ли кусок отмененной операции
        if (data.is_preload) {
            if (data.request_id !== preloadRequestId) {
                logPlayer("MISMATCH", "Отброшена устаревшая предзагрузка.", { id: data.request_id });
                return;
            }
        } else {
            if (data.request_id !== seekRequestId) {
                logPlayer("MISMATCH", "Отброшен устаревший фрагмент перемотки.", { id: data.request_id });
                return;
            }
        }

        const url = `http://${window.location.host}/video?path=${encodeURIComponent(data.relative_path)}&t=${Date.now()}`;
        const chunkStart = data.start_time;

        if (!data.is_preload) {
            // --- DIRECT PLAY (Seek) ---
            logPlayer("PLAY", `Загружаем фрагмент (Seek).`, { time: chunkStart });
            document.getElementById('loading-overlay').style.display = 'none';
            this.isLoading = false;
            this.currentChunkStartTime = chunkStart;
            this.currentPlayer._chunkStart = chunkStart;
            this.currentPlayer.src = url;
            this.currentPlayer.load();

            this.currentPlayer.onloadedmetadata = () => {
                this.currentPlayer.currentTime = 0;
                if (this.isPlaying) this.currentPlayer.play().catch(e => console.log(e));
            };
        } else {
            // --- PRELOAD BUFFER ---
            // СТРОГАЯ МАТЕМАТИКА: ожидаем строго текущий старт + размер фрагмента (10.0)
            const expectedNext = this.currentChunkStartTime + this.fragmentDuration;
            if (Math.abs(chunkStart - expectedNext) < 2.0) {
                logPlayer("PLAY (Buffer)", "Предзагруженный фрагмент сохранен.", { time: chunkStart });
                this.nextPlayer.src = url;
                this.nextPlayer.load();
                this.nextPlayer._chunkStart = chunkStart;
                this.nextChunkRequested = false;

                if (this.isLoading) {
                    logPlayer("PLAY", "Отложенный Swap: Буфер прибыл, запускаем.");
                    this.swap(); 
                }
            } else {
                logPlayer("MISMATCH", "Предзагрузка пришла, но время не стыкуется.", { chunkStart, expectedNext });
                this.nextChunkRequested = false; 
            }
        }
    },

    hasNextChunkBuffered: function () {
        if (!this.currentPlayer) return false;
        // СТРОГАЯ МАТЕМАТИКА
        const nextStartTime = this.currentChunkStartTime + this.fragmentDuration;
        return Math.abs(this.nextPlayer._chunkStart - nextStartTime) < 1.0 && this.nextPlayer.readyState >= 0;
    },

    onTimeUpdate: function () {
        if (!this.isPlaying || this.isLoading) return; 
        
        const playerTime = this.currentPlayer.currentTime;
        if (!isFinite(playerTime)) return;


        if (playerTime >= this.fragmentDuration && (this.currentChunkStartTime + this.fragmentDuration) < this.totalDuration) {
            this.swap();
            return;
        }

        const globalTime = this.currentChunkStartTime + playerTime;

        // Timeline UI
        if (this.totalDuration > 0) {
            const percent = (globalTime / this.totalDuration) * 100;
            document.getElementById('timeline-cursor').style.left = percent + '%';
            document.getElementById('v-time').textContent = `${formatTime(globalTime)} / ${formatTime(this.totalDuration)}`;
        }

        // PiP Logic
        const activeClip = this.clips.find(c => globalTime >= c.global_start && globalTime < (c.global_start + c.duration));
        if (activeClip && activeClip.has_overlay && activeClip.segment_id && !isDraggingBox && !isResizingBox) {
            showPipBox(activeClip.segment_id, activeClip.overlay_coords);
        } else if (!activeClip || !activeClip.has_overlay) {
            if (!isDraggingBox && !isResizingBox) hidePipBox();
        }

        // Preload Logic (Основанная на строгом шаге)
        const remaining = this.fragmentDuration - playerTime;
        if (remaining < 5 && !this.nextChunkRequested && !this.hasNextChunkBuffered() && (this.currentChunkStartTime + this.fragmentDuration) < this.totalDuration - 0.5) {
            const nextStart = this.currentChunkStartTime + this.fragmentDuration;
            this.nextChunkRequested = true;
            this.nextChunkStartTime = nextStart;

            requestPreviewFragment(nextStart, "Авто-предзагрузка (Next Chunk)", true);
        }
    },
swap: function () {
        // Проверка абсолютного окончания всего видео
        if (this.currentChunkStartTime + this.currentPlayer.currentTime >= this.totalDuration - 0.5) {
            logPlayer("PLAY", "Конец видео.");
            this.isPlaying = false;
            document.getElementById('v-play-btn').textContent = '▶';
            this.currentPlayer.pause();
            return;
        }

        const expectedNextTime = this.currentChunkStartTime + this.fragmentDuration;

        const isNextReady = this.nextPlayer.readyState >= 2 || (this.nextPlayer.readyState >= 0 && this.nextPlayer.currentSrc);
        const isTimeCorrect = Math.abs(this.nextPlayer._chunkStart - expectedNextTime) < 1.0;

        if (isNextReady && isTimeCorrect) {
            logPlayer("PLAY", "Swap: Успешный переход.", { nextStart: this.nextPlayer._chunkStart });

            if (this.isLoading) {
                document.getElementById('loading-overlay').style.display = 'none';
                this.isLoading = false;
            }

            this.currentPlayer.style.display = 'none';
            this.nextPlayer.style.display = 'block';

            this.currentChunkStartTime = expectedNextTime; 
            
            this.nextPlayer.currentTime = 0;
            this.nextPlayer.play().catch(e => console.log(e));
            this.currentPlayer.pause();

            const temp = this.currentPlayer;
            this.currentPlayer = this.nextPlayer;
            this.nextPlayer = temp;

            this.currentPlayer.onended = () => this.swap();
            this.currentPlayer.ontimeupdate = () => this.onTimeUpdate();
            this.nextPlayer.onended = null;
            this.nextPlayer.ontimeupdate = null;

            this.nextPlayer.removeAttribute('src');
            this.nextPlayer._chunkStart = -1;
            this.nextPlayer.load();
            this.nextChunkRequested = false;

        } else {
            if (this.nextChunkRequested && Math.abs(this.nextChunkStartTime - expectedNextTime) < 1.0) {
                logPlayer("PLAY", "Swap: Ждем уже запрошенный буфер (не спамим сервер).", { expected: expectedNextTime });
                this.isLoading = true;
                document.getElementById('loading-overlay').style.display = 'flex';
            } else {
                logPlayer("PLAY", "Swap: Буфер потерян. Принудительная загрузка.", { expected: expectedNextTime });
                this.isLoading = true;
                document.getElementById('loading-overlay').style.display = 'flex';
                this.seek(expectedNextTime, "Swap Fail Recovery");
            }
        }
    }
};

window.onload = () => { connectWS(); addSegment(); streamer.init(); setupTimelineInteraction(); setupPipInteraction(); };

function connectWS() {
    ws = new WebSocket('ws://' + window.location.host);
    ws.onopen = () => console.log('WS Connected');
    ws.onmessage = (e) => handleWSMessage(JSON.parse(e.data));
    ws.onclose = () => setTimeout(connectWS, 2000);
}

function handleWSMessage(data) {
    if (data.action === 'log') {
        const log = document.getElementById('log-output');
        if (data.type === 'progress') {
            const lines = log.innerHTML.split('\n');
            if (lines.length > 1 && lines[lines.length - 2].includes('id="cur-progress"')) {
                lines[lines.length - 2] = `<span id="cur-progress">${data.message}</span>`;
                log.innerHTML = lines.join('\n');
            } else log.innerHTML += `<span id="cur-progress">${data.message}</span>\n`;
        } else {
            let style = data.type === 'header' ? 'style="color: #00bbff; font-weight: bold;"' : "";
            log.innerHTML += `<span ${style}>${data.message}</span>\n`;
        }
        log.scrollTop = log.scrollHeight;
    } else if (data.action === 'config_info') {
        if (data.default_intro) { globalSettings.introPath = data.default_intro; document.getElementById('intro_file_path').textContent = data.default_intro + " (Стандартное)"; }
        document.getElementById('output_dir_path').textContent = data.default_output;
        globalSettings.outputDir = data.default_output;
    } else if (data.action === 'browse_result') { renderFileList(data); }
    else if (data.action === 'path_resolved') { applySelectedPath(data.full_path); }
    else if (data.action === 'preview_map_ready') {
        streamer.totalDuration = data.total_duration;
        streamer.fragmentDuration = data.fragment_duration || 10.0; // ДОБАВИТЬ ЭТО
        streamer.clips = data.clips;
        renderTimelineVisual(data.clips, data.total_duration);
        document.querySelector('.virtual-player-container').style.display = 'block';
        document.getElementById('v-time').textContent = `00:00 / ${formatTime(data.total_duration)}`;
    } else if (data.action === 'preview_fragment_ready') { streamer.onChunkReady(data); }
    else if (data.action === 'finished') {
        document.getElementById('submitBtn').disabled = false;
        document.getElementById('submitBtn').textContent = 'Начать Обработку';
    }
}

// --- UI Functions (Add/Remove Segments, etc) ---
function addSegment() {
    if (segments.length >= 10) return alert("Максимум 10 сегментов");
    const id = Date.now().toString();
    const visualNum = segments.length + 2;
    let defaultVideo = segments.length > 0 ? segments[segments.length - 1].videoPath : '';
    const segObj = { id, videoPath: defaultVideo, audioPath: '', overlayPath: '', start: '', end: '', pip_start: '', pip_end: '', pip_x: 0.7, pip_y: 0.7, pip_w: 0.25 };
    segments.push(segObj);
    const div = document.createElement('div');
    div.className = 'segment-block'; div.id = `seg-${id}`;
    div.innerHTML = `
                <div class="segment-header">
                    <span class="segment-title">${visualNum}. Фрагмент видео</span>
                    <button class="remove-seg-btn" onclick="removeSegment('${id}')">✕</button>
                </div>
                <div>
                    <button onclick="browse('${id}', 'video')">📹 Основное видео...</button>
                    <div id="path-video-${id}" class="file-path-display">${defaultVideo || '(не выбрано)'}</div>
                </div>
                <div style="margin-top:10px;">
                    <div class="time-input-group">
                        <label style="width:60px;">Начало:</label>
                        <input type="text" id="start-${id}" placeholder="00:00:00" onchange="updateSeg('${id}', 'start', this.value)">
                        <button class="jump-btn" onclick="previewJump('${id}', 'start')">▶</button>
                    </div>
                    <div class="time-input-group">
                        <label style="width:60px;">Конец:</label>
                        <input type="text" id="end-${id}" placeholder="Конец файла" onchange="updateSeg('${id}', 'end', this.value)">
                        <button class="jump-btn" onclick="previewJump('${id}', 'end')">▶</button>
                    </div>
                </div>
                <div style="margin-top:10px; padding: 10px; background: #333; border-radius: 4px; border: 1px dashed #555;">
                    <label><input type="checkbox" id="pip-enable-${id}" onchange="togglePiP('${id}')"> 📸 Поверх экрана (Камера PiP)</label>
                    <div id="pip-panel-${id}" class="hidden" style="margin-top: 10px; border-top: 1px solid #444; padding-top: 10px;">
                        <button onclick="browse('${id}', 'overlay')" style="background: var(--border-color);">Выбрать видео камеры...</button>
                        <div id="path-overlay-${id}" class="file-path-display" style="border:none; background:#222;">(не выбрано)</div>
                        <div style="display: flex; gap: 10px; margin-top: 10px; margin-bottom: 10px;">
                            <input type="text" id="pip-start-${id}" placeholder="Старт камеры (00:00)" onchange="updateSeg('${id}', 'pip_start', this.value)" style="flex:1;">
                            <input type="text" id="pip-end-${id}" placeholder="Конец камеры (Опц.)" onchange="updateSeg('${id}', 'pip_end', this.value)" style="flex:1;">
                        </div>
                        <div style="font-size: 0.85em; color: #aaa; margin-bottom: 10px;">ℹ️ Разместите и измените размер камеры прямо на окне предпросмотра справа.</div>
                        <label><input type="checkbox" id="pip-audio-${id}" onchange="requestPreviewMap()"> Микшировать звук камеры с основным</label>
                    </div>
                </div>`;
    document.getElementById('segments-container').appendChild(div);
}

function removeSegment(id) {
    segments = segments.filter(s => s.id !== id);
    document.getElementById(`seg-${id}`).remove();
    document.querySelectorAll('#segments-container .segment-title').forEach((el, idx) => el.textContent = `${idx + 2}. Фрагмент видео`);
    if (activePipSegmentId === id) hidePipBox();
    requestPreviewMap();
}

function updateSeg(id, field, value) {
    const seg = segments.find(s => s.id === id);
    if (seg) seg[field] = value;
    requestPreviewMap();
}

function togglePiP(id) {
    const enabled = document.getElementById(`pip-enable-${id}`).checked;
    const panel = document.getElementById(`pip-panel-${id}`);
    if (enabled) panel.classList.remove('hidden'); else panel.classList.add('hidden');
    if (!enabled && activePipSegmentId === id) hidePipBox();
    requestPreviewMap();
}

function updateVol() {
    globalSettings.volume = document.getElementById('vol_slider').value;
    document.getElementById('vol_val').textContent = globalSettings.volume + 'x';
    requestPreviewMap();
}

function browse(segId, type) {
    currentBrowseId = { segId, type };
    document.getElementById('file-modal').classList.remove('hidden');
    document.getElementById('select-dir-confirm').classList.add('hidden');
    if (type === 'output') document.getElementById('select-dir-confirm').classList.remove('hidden');
    ws.send(JSON.stringify({ action: 'browse_path', path: '/', id: type === 'output' ? 'output_dir' : 'input' }));
}
function selectFile(type) { browse(null, type === 'intro_file' ? 'intro' : 'output'); }
function selectDirectory(type) { browse(null, 'output'); }

const pipBox = document.getElementById('pip-overlay-box');
const videoWrapper = document.getElementById('video-wrapper');
let isDraggingBox = false; let isResizingBox = false;
let startX, startY, startLeft, startTop, startWidth;

function showPipBox(segId, coords) {
    if (!coords) return;
    activePipSegmentId = segId;
    pipBox.style.display = 'block';
    pipBox.style.left = (coords.x * 100) + '%';
    pipBox.style.top = (coords.y * 100) + '%';
    pipBox.style.width = (coords.w * 100) + '%';
    pipBox.style.height = (coords.w * 100) + '%';
    document.getElementById('pip-hint-text').textContent = `Камера (Сегм ${segments.findIndex(s => s.id === segId) + 1})`;
}
function hidePipBox() { activePipSegmentId = null; pipBox.style.display = 'none'; }

function updateLocalStateFromDom() {
    if (!activePipSegmentId) return;
    const x = parseFloat(pipBox.style.left) / 100;
    const y = parseFloat(pipBox.style.top) / 100;
    const w = parseFloat(pipBox.style.width) / 100;
    const seg = segments.find(s => s.id === activePipSegmentId);
    if (seg) { seg.pip_x = x; seg.pip_y = y; seg.pip_w = w; }
    const activeClip = streamer.clips.find(c => c.segment_id === activePipSegmentId);
    if (activeClip && activeClip.overlay_coords) { activeClip.overlay_coords.x = x; activeClip.overlay_coords.y = y; activeClip.overlay_coords.w = w; }
}

function triggerUpdateDebounced(reason, force = false) {
    if (dragDebounceTimer) clearTimeout(dragDebounceTimer);

    const delay = force ? 0 : 250;

    dragDebounceTimer = setTimeout(() => {
        const currentTimeExact = streamer.currentChunkStartTime + streamer.currentPlayer.currentTime;
        ws.send(JSON.stringify({ action: 'generate_preview_map', params: gatherParams() }));
        streamer.seek(currentTimeExact, reason);
    }, delay);
}

function setupPipInteraction() {
    const handle = document.getElementById('pip-handle-se');

    pipBox.addEventListener('mousedown', (e) => {
        if (e.target === handle) return;
        isDraggingBox = true;
        startX = e.clientX; startY = e.clientY;
        startLeft = parseFloat(pipBox.style.left) || 0;
        startTop = parseFloat(pipBox.style.top) || 0;
    });

    handle.addEventListener('mousedown', (e) => {
        isResizingBox = true;
        e.stopPropagation();
        startX = e.clientX;
        startWidth = parseFloat(pipBox.style.width) || 0;
    });

    document.addEventListener('mousemove', (e) => {
        if (isDraggingBox || isResizingBox) {
            const rect = videoWrapper.getBoundingClientRect();
            if (isDraggingBox) {
                const dx = ((e.clientX - startX) / rect.width) * 100;
                const dy = ((e.clientY - startY) / rect.height) * 100;
                pipBox.style.left = Math.max(0, Math.min(startLeft + dx, 100 - parseFloat(pipBox.style.width))) + '%';
                pipBox.style.top = Math.max(0, Math.min(startTop + dy, 100 - parseFloat(pipBox.style.height))) + '%';
            } else {
                const dx = ((e.clientX - startX) / rect.width) * 100;
                let newWidth = Math.max(5, Math.min(startWidth + dx, 100 - parseFloat(pipBox.style.left)));
                pipBox.style.width = newWidth + '%'; pipBox.style.height = newWidth + '%';
            }
            updateLocalStateFromDom();
            triggerUpdateDebounced("PiP Moving");
        }
    });

    document.addEventListener('mouseup', () => {
        if (isDraggingBox || isResizingBox) {
            isDraggingBox = false; isResizingBox = false;
            updateLocalStateFromDom();
            triggerUpdateDebounced("PiP Finish", true);
        }
    });
}

function gatherParams() {
    return {
        use_ram: document.getElementById('use_ram').checked,
        intro_resolution: document.querySelector('input[name="intro_res"]:checked').value,
        intro_file: globalSettings.introPath,
        output_dir: globalSettings.outputDir,
        volume: globalSettings.volume,
        segments_list: segments.map(s => {
            const pipEnabled = document.getElementById(`pip-enable-${s.id}`)?.checked;
            const pipData = pipEnabled ? { video: s.overlayPath, start: s.pip_start, end: s.pip_end, x: s.pip_x, y: s.pip_y, w: s.pip_w, mix_audio: document.getElementById(`pip-audio-${s.id}`).checked } : null;
            return { id: s.id, video: s.videoPath, audio: s.audioPath || s.videoPath, start: s.start, end: s.end, overlay: pipData };
        })
    };
}

function submitForm() {
    if (segments.length === 0 || !segments[0].videoPath) return alert('Выберите хотя бы один фрагмент!');
    document.getElementById('submitBtn').disabled = true; document.getElementById('submitBtn').textContent = 'Обработка...';
    ws.send(JSON.stringify({ action: 'process', params: gatherParams() }));
}

function requestPreviewMap() { if (segments.some(s => s.videoPath)) ws.send(JSON.stringify({ action: 'generate_preview_map', params: gatherParams() })); }
function renderFileList(data) { const list = document.getElementById('file-list'); list.innerHTML = ''; document.getElementById('current-path').value = data.path; if (data.path !== '/') { const up = document.createElement('div'); up.className = 'file-entry'; up.textContent = '.. (Назад)'; up.onclick = () => ws.send(JSON.stringify({ action: 'browse_path', path: data.path + '/..', id: 'nav' })); list.appendChild(up); } data.entries.forEach(entry => { const div = document.createElement('div'); div.className = 'file-entry'; div.textContent = (entry.type === 'dir' ? '📁 ' : '📄 ') + entry.name; div.onclick = () => { if (entry.type === 'dir') ws.send(JSON.stringify({ action: 'browse_path', path: data.path + '/' + entry.name, id: 'nav' })); else ws.send(JSON.stringify({ action: 'resolve_path', path: data.path + '/' + entry.name, id: 'file' })); }; list.appendChild(div); }); document.getElementById('select-dir-confirm').onclick = () => ws.send(JSON.stringify({ action: 'resolve_path', path: data.path, id: 'dir' })); }
function applySelectedPath(path) { document.getElementById('file-modal').classList.add('hidden'); if (currentBrowseId.type === 'intro') { globalSettings.introPath = path; document.getElementById('intro_file_path').textContent = path; } else if (currentBrowseId.type === 'output') { globalSettings.outputDir = path; document.getElementById('output_dir_path').textContent = path; } else { const seg = segments.find(s => s.id === currentBrowseId.segId); if (seg) { if (currentBrowseId.type === 'video') { seg.videoPath = path; document.getElementById(`path-video-${seg.id}`).textContent = path; } else if (currentBrowseId.type === 'overlay') { seg.overlayPath = path; document.getElementById(`path-overlay-${seg.id}`).textContent = path; } } } requestPreviewMap(); }
function renderTimelineVisual(clips, totalDuration) { const trackContainer = document.getElementById('timeline-tracks'); trackContainer.innerHTML = ''; clips.forEach(clip => { const block = document.createElement('div'); block.className = `timeline-block color-${clip.color}`; if (clip.has_overlay) block.style.backgroundColor = 'var(--accent-pink)'; block.style.width = (clip.duration / totalDuration) * 100 + '%'; block.title = `${clip.name} (${formatTime(clip.duration)})`; block.innerHTML = `<span class="block-label">${clip.name}</span>`; trackContainer.appendChild(block); }); }
function setupTimelineInteraction() {
    const timeline = document.getElementById('timeline-visual');
    let isDragging = false;
    let scrubTimer = null;
    let lastSentTime = -1; // Щит от одинаковых дублирующихся запросов

    const updateVisuals = (e) => {
        const rect = timeline.getBoundingClientRect();
        const x = Math.max(0, Math.min(e.clientX - rect.left, rect.width));
        const time = (x / rect.width) * streamer.totalDuration;

        // Визуально ползунок бегает моментально при любом движении
        if (streamer.totalDuration > 0) {
            const percent = (time / streamer.totalDuration) * 100;
            document.getElementById('timeline-cursor').style.left = percent + '%';
            document.getElementById('v-time').textContent = `${formatTime(time)} / ${formatTime(streamer.totalDuration)}`;
        }
        return time;
    };

    const triggerSeek = (time, reason) => {
        // Если мы уже только что запросили ровно это же время, игнорируем (погрешность 0.1 сек)
        if (Math.abs(lastSentTime - time) < 0.1) return;

        lastSentTime = time;
        streamer.seek(time, reason);
    };

    // 1. НАЖАТИЕ: Ничего не просим у сервера, только обновляем картинку ползунка
    timeline.addEventListener('mousedown', (e) => {
        if (e.button !== 0) return; // Реагируем только на ЛКМ
        isDragging = true;
        updateVisuals(e);
    });

    // 2. ДВИЖЕНИЕ МЫШИ (Перетаскивание ползунка с зажатой кнопкой)
    document.addEventListener('mousemove', (e) => {
        if (!isDragging) return;
        const time = updateVisuals(e);

        if (scrubTimer) clearTimeout(scrubTimer);
        // Дергаем сервер только если пользователь при перетаскивании остановил мышь на 300мс
        scrubTimer = setTimeout(() => triggerSeek(time, "Timeline Drag"), 300);
    });

    // 3. ОТПУСКАНИЕ (Конец клика или перетаскивания)
    document.addEventListener('mouseup', (e) => {
        if (!isDragging) return;
        isDragging = false;
        const time = updateVisuals(e);

        // Отменяем таймер движения, чтобы запросы не скрестились
        if (scrubTimer) clearTimeout(scrubTimer);

        // Гарантированно шлем ровно ОДИН финальный запрос
        triggerSeek(time, "Timeline Drop");
    });
}
function previewJump(segId, field) { const segIdx = segments.findIndex(s => s.id === segId); const targetNameStart = `Seg${segIdx + 1}`; const targetClip = streamer.clips.find(c => c.name.startsWith(targetNameStart)); if (targetClip) { let time = targetClip.global_start; if (field === 'end') { const lastClip = streamer.clips.slice().reverse().find(c => c.name.startsWith(targetNameStart)); if (lastClip) time = lastClip.global_start + lastClip.duration - 5; } streamer.seek(time, "Jump Button"); } }
function formatTime(s) { const m = Math.floor(s / 60); const sec = Math.floor(s % 60); return `${m}:${sec.toString().padStart(2, '0')}`; }
document.getElementById('close-modal').onclick = () => document.getElementById('file-modal').classList.add('hidden');
document.getElementById('up-btn').onclick = () => ws.send(JSON.stringify({ action: 'browse_path', path: document.getElementById('current-path').value + '/..', id: 'nav' }));
