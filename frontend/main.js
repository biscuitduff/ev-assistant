'use strict';

// E.V. browser frontend for the Docker/local-AI edition.
// Backend protocol: FastAPI WebSocket /ws + HTTP /stats.

const vizCanvas = document.getElementById('viz');
const statusEl = document.getElementById('status');
const transcript = document.getElementById('transcript');

if (!vizCanvas || !statusEl || !transcript) {
    throw new Error('E.V. frontend is missing required DOM elements.');
}

// ---------------------------------------------------------------------------
// Language / labels
// ---------------------------------------------------------------------------

const langOverride = new URLSearchParams(window.location.search).get('lang');
let appLang = langOverride === 'tr' ? 'tr' : 'en';
let serverVoice = 'kokoro';
let i18nApplied = false;

const I18N = {
    tr: {
        sys: '// SİSTEM',
        modules: '// MODÜLLER',
        workshop: '// ATÖLYE',
        log: '// OLAY GÜNLÜĞÜ',
        mem: 'BELLEK',
        wind: 'RÜZGÂR',
        core: 'ÇEKİRDEK',
        voice: 'SES',
        send: 'GÖNDER',
        start: 'E.V. bağlantısı kuruluyor…',
        ph: "E.V.'ye yaz…",
    },
};

const STR = {
    en: {
        connected: 'Connected · type a message or press MIC',
        start: 'Click MIC / Ctrl+Space to listen',
        muted: 'Muted · press MIC or Ctrl+Space',
        listening: 'Listening…',
        thinking: 'E.V. is thinking…',
        speaking: 'E.V. is speaking…',
        timeout: 'Response is taking a while. You can try again.',
        disconnected: 'Connection lost · reconnecting…',
        wsError: 'WebSocket connection error.',
        micPerm: 'Microphone permission is required.',
        micSecure: 'Microphone requires HTTPS (or localhost). Text chat still works here.',
        micPrep: 'Preparing microphone…',
        idleMuted: 'Muted after inactivity · press MIC to resume',
        idleLog: 'Conversation mode muted after inactivity.',
        visionOn: 'Screen vision enabled.',
        visionOff: 'Screen vision disabled.',
        visionSecure: 'Screen sharing requires HTTPS (or localhost).',
        visionDenied: 'Screen sharing was not enabled.',
        noSocket: 'Not connected to E.V. yet.',
        audioBlocked: 'Browser blocked audio playback. Click anywhere, then try again.',
        yes: 'Yes',
        no: 'No',
        fsIn: 'Fullscreen detected; E.V. was hidden.',
        fsOut: 'Fullscreen ended; E.V. is visible again.',
        stopped: 'Stopped local playback.',
        started: 'E.V. browser console initialized.',
        sleeping: 'E.V. is sleeping. Send a message or press MIC to wake it.',
    },
    tr: {
        connected: 'Bağlandı · mesaj yaz veya MIC düğmesine bas',
        start: 'Dinlemek için MIC / Ctrl+Space',
        muted: 'Sessiz · MIC veya Ctrl+Space',
        listening: 'Dinliyorum…',
        thinking: 'E.V. düşünüyor…',
        speaking: 'E.V. konuşuyor…',
        timeout: 'Yanıt gecikiyor. Tekrar deneyebilirsin.',
        disconnected: 'Bağlantı koptu · yeniden bağlanıyor…',
        wsError: 'WebSocket bağlantı hatası.',
        micPerm: 'Mikrofon izni gerekli.',
        micSecure: 'Mikrofon HTTPS (veya localhost) gerektirir. Metin sohbeti çalışır.',
        micPrep: 'Mikrofon hazırlanıyor…',
        idleMuted: 'Boşta kaldığı için sessize alındı · MIC ile devam et',
        idleLog: 'Konuşma modu boşta kaldığı için sessize alındı.',
        visionOn: 'Ekran görüşü açıldı.',
        visionOff: 'Ekran görüşü kapatıldı.',
        visionSecure: 'Ekran paylaşımı HTTPS (veya localhost) gerektirir.',
        visionDenied: 'Ekran paylaşımı açılmadı.',
        noSocket: 'E.V. bağlantısı henüz kurulmadı.',
        audioBlocked: 'Tarayıcı ses oynatmayı engelledi. Sayfaya tıklayıp tekrar dene.',
        yes: 'Evet',
        no: 'Hayır',
        fsIn: 'Tam ekran algılandı; E.V. gizlendi.',
        fsOut: 'Tam ekran bitti; E.V. geri geldi.',
        stopped: 'Yerel ses oynatma durduruldu.',
        started: 'E.V. tarayıcı konsolu başlatıldı.',
        sleeping: 'E.V. uyuyor. Uyandırmak için mesaj gönder veya MIC kullan.',
    },
};

function T(key) {
    return (STR[appLang] || STR.en)[key] || key;
}

function applyI18n() {
    document.documentElement.lang = appLang;
    const dict = I18N[appLang];
    if (!dict) return;

    document.querySelectorAll('[data-i18n]').forEach((el) => {
        const key = el.getAttribute('data-i18n');
        if (key && dict[key]) el.textContent = dict[key];
    });

    document.querySelectorAll('[data-i18n-ph]').forEach((el) => {
        const key = el.getAttribute('data-i18n-ph');
        if (key && dict[key]) el.placeholder = dict[key];
    });
}

// ---------------------------------------------------------------------------
// General state
// ---------------------------------------------------------------------------

let orbState = 'idle';
let ws = null;
let reconnectTimer = null;
let watchdog = null;
let awaitingResponse = false;
let streamingResponse = false;
let currentEvDiv = null;

let audioUnlocked = false;
let audioQueue = [];
let isPlaying = false;
let currentAudio = null;
let ttsSpeaking = false;

let listeningEnabled = false;
let lastActivity = Date.now();
const IDLE_MUTE_MS = 120000;

function markActivity() {
    lastActivity = Date.now();
}

function socketOpen() {
    return ws && ws.readyState === WebSocket.OPEN;
}

function busy() {
    return awaitingResponse || streamingResponse || isPlaying || ttsSpeaking;
}

function setAwaiting(value) {
    awaitingResponse = Boolean(value);

    if (watchdog) {
        clearTimeout(watchdog);
        watchdog = null;
    }

    if (awaitingResponse) {
        watchdog = setTimeout(() => {
            awaitingResponse = false;
            statusEl.textContent = T('timeout');
            resumeListening();
        }, 90000);
    }
}

// ---------------------------------------------------------------------------
// Transcript / confirmations
// ---------------------------------------------------------------------------

function addTranscript(role, text) {
    if (!text) return;
    const div = document.createElement('div');
    div.className = role;
    div.textContent = text;
    transcript.appendChild(div);
    transcript.scrollTop = transcript.scrollHeight;
}

function logSys(text) {
    const locale = appLang === 'tr' ? 'tr-TR' : 'en-US';
    const time = new Date().toLocaleTimeString(locale, {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
    });

    const div = document.createElement('div');
    div.className = 'sys';
    div.textContent = `[${time}] ${text}`;
    transcript.appendChild(div);
    transcript.scrollTop = transcript.scrollHeight;
}

function sendConfirm(approved) {
    if (!socketOpen()) {
        logSys(T('noSocket'));
        return;
    }
    ws.send(JSON.stringify({ confirm: Boolean(approved) }));
}

function addConfirm(text) {
    const div = document.createElement('div');
    div.className = 'ev confirm';

    const prompt = document.createElement('span');
    prompt.textContent = `${text || ''} `;

    const yes = document.createElement('button');
    yes.type = 'button';
    yes.className = 'confirm-btn yes';
    yes.textContent = T('yes');

    const no = document.createElement('button');
    no.type = 'button';
    no.className = 'confirm-btn no';
    no.textContent = T('no');

    const done = (approved) => {
        yes.disabled = true;
        no.disabled = true;
        sendConfirm(approved);
    };

    yes.addEventListener('click', () => done(true));
    no.addEventListener('click', () => done(false));

    div.appendChild(prompt);
    div.appendChild(yes);
    div.appendChild(no);
    transcript.appendChild(div);
    transcript.scrollTop = transcript.scrollHeight;
}

// ---------------------------------------------------------------------------
// WebSocket
// ---------------------------------------------------------------------------

function websocketUrl() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${protocol}//${window.location.host}/ws`;
}

function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
    }, 3000);
}

function connect() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        return;
    }

    setOrbState('thinking');
    statusEl.textContent = T('disconnected');

    try {
        ws = new WebSocket(websocketUrl());
    } catch (error) {
        console.error('[E.V.] Could not create WebSocket:', error);
        statusEl.textContent = T('wsError');
        scheduleReconnect();
        return;
    }

    ws.addEventListener('open', () => {
        console.log('[E.V.] WebSocket connected:', websocketUrl());
        if (reconnectTimer) {
            clearTimeout(reconnectTimer);
            reconnectTimer = null;
        }

        setAwaiting(false);
        setOrbState('idle');
        statusEl.textContent = T('connected');
        logSys(`WebSocket connected: ${websocketUrl()}`);
    });

    ws.addEventListener('message', (event) => {
        let data;
        try {
            data = JSON.parse(event.data);
        } catch (error) {
            console.error('[E.V.] Invalid WebSocket JSON:', error, event.data);
            return;
        }

        markActivity();
        handleServerMessage(data).catch((error) => {
            console.error('[E.V.] Message handling error:', error);
            logSys(`Message handling error: ${error.message || error}`);
        });
    });

    ws.addEventListener('close', (event) => {
        console.warn('[E.V.] WebSocket closed:', event.code, event.reason || '');
        setAwaiting(false);
        setOrbState('muted');
        statusEl.textContent = T('disconnected');
        scheduleReconnect();
    });

    ws.addEventListener('error', (event) => {
        console.error('[E.V.] WebSocket error:', event);
        statusEl.textContent = T('wsError');
    });
}

async function handleServerMessage(data) {
    switch (data.type) {
        case 'screen_request':
            setOrbState('thinking');
            setAwaiting(true);
            await sendVisionFrame();
            return;

        case 'response':
            streamingResponse = false;
            if (data.text) addTranscript('ev', data.text);
            setAwaiting(false);

            if (data.audio) {
                queueAudio(data.audio);
            } else if (serverVoice === 'browser' && data.text) {
                speakBrowser(data.text);
            } else {
                resumeListening();
            }
            return;

        case 'response_chunk':
            streamingResponse = true;
            setAwaiting(false);

            if (data.text) {
                if (!currentEvDiv) {
                    currentEvDiv = document.createElement('div');
                    currentEvDiv.className = 'ev';
                    transcript.appendChild(currentEvDiv);
                }

                currentEvDiv.textContent +=
                    (currentEvDiv.textContent ? ' ' : '') + data.text;
                transcript.scrollTop = transcript.scrollHeight;
            }

            if (data.audio) queueAudio(data.audio);
            return;

        case 'response_done':
            streamingResponse = false;
            currentEvDiv = null;
            setAwaiting(false);
            if (!isPlaying && !ttsSpeaking && audioQueue.length === 0) {
                resumeListening();
            }
            return;

        case 'confirm':
            streamingResponse = false;
            setAwaiting(false);
            addConfirm(data.text || 'Confirm?');
            if (data.audio) {
                queueAudio(data.audio);
            } else if (serverVoice === 'browser' && data.text) {
                speakBrowser(data.text);
            }
            return;

        case 'sleep':
            listeningEnabled = false;
            setOrbState('muted');
            statusEl.textContent = T('sleeping');
            return;

        case 'fullscreen':
            handleFullscreen(Boolean(data.active));
            return;

        case 'user_text':
            if (data.text) addTranscript('user', data.text);
            return;

        case 'idle':
            streamingResponse = false;
            setAwaiting(false);
            resumeListening();
            return;

        case 'status':
            if (data.text) statusEl.textContent = data.text;
            return;

        default:
            console.debug('[E.V.] Unhandled server message:', data);
    }
}

// ---------------------------------------------------------------------------
// Text input
// ---------------------------------------------------------------------------

const STOP_WORDS = /^(dur|durdur|dursana|kes|sus|stop|shut up|be quiet|cancel)\.?$/i;

function sendText() {
    const input = document.getElementById('text-input');
    if (!input) return;

    const text = input.value.trim();
    if (!text) return;

    if (STOP_WORDS.test(text)) {
        input.value = '';
        stopEverything();
        return;
    }

    if (!socketOpen()) {
        statusEl.textContent = T('noSocket');
        logSys(T('noSocket'));
        connect();
        return;
    }

    input.value = '';
    addTranscript('user', text);
    setOrbState('thinking');
    statusEl.textContent = T('thinking');
    setAwaiting(true);
    markActivity();

    ws.send(JSON.stringify({ text }));
}

// ---------------------------------------------------------------------------
// Audio playback / browser-TTS fallback
// ---------------------------------------------------------------------------

async function unlockAudio() {
    if (audioUnlocked) return;
    audioUnlocked = true;

    try {
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (Ctx) {
            const ctx = new Ctx();
            if (ctx.state === 'suspended') await ctx.resume();
            await ctx.close();
        }
    } catch (error) {
        console.debug('[E.V.] Audio unlock:', error);
    }
}

function queueAudio(base64Audio) {
    if (!base64Audio) return;
    audioQueue.push(base64Audio);
    if (!isPlaying) playNextAudio();
}

function playNextAudio() {
    if (audioQueue.length === 0) {
        isPlaying = false;
        currentAudio = null;
        resumeListening();
        return;
    }

    isPlaying = true;
    setOrbState('speaking');
    statusEl.textContent = T('speaking');

    const base64Audio = audioQueue.shift();
    let bytes;

    try {
        bytes = Uint8Array.from(atob(base64Audio), (c) => c.charCodeAt(0));
    } catch (error) {
        console.error('[E.V.] Invalid base64 audio:', error);
        playNextAudio();
        return;
    }

    const url = URL.createObjectURL(new Blob([bytes], { type: 'audio/mpeg' }));
    const audio = new Audio(url);
    currentAudio = audio;
    hookOutputAnalyser(audio);

    const finish = () => {
        URL.revokeObjectURL(url);
        if (currentAudio === audio) currentAudio = null;
        playNextAudio();
    };

    audio.addEventListener('ended', finish, { once: true });
    audio.addEventListener('error', finish, { once: true });

    audio.play().catch((error) => {
        console.warn('[E.V.] Audio playback blocked/failed:', error);
        URL.revokeObjectURL(url);
        if (currentAudio === audio) currentAudio = null;
        audioQueue = [];
        isPlaying = false;
        statusEl.textContent = T('audioBlocked');
        logSys(T('audioBlocked'));
    });
}

function pickBrowserVoice() {
    if (!('speechSynthesis' in window)) return null;
    const prefix = appLang === 'tr' ? 'tr' : 'en';
    const voices = window.speechSynthesis.getVoices() || [];
    const candidates = voices.filter(
        (voice) => voice.lang && voice.lang.toLowerCase().startsWith(prefix),
    );
    return candidates.find((voice) => /natural|neural/i.test(voice.name)) || candidates[0] || null;
}

function speakBrowser(text) {
    if (serverVoice !== 'browser' || !text || !('speechSynthesis' in window)) {
        resumeListening();
        return;
    }

    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = appLang === 'tr' ? 'tr-TR' : 'en-US';
    const voice = pickBrowserVoice();
    if (voice) utterance.voice = voice;

    ttsSpeaking = true;
    setOrbState('speaking');
    statusEl.textContent = T('speaking');

    utterance.addEventListener('end', () => {
        ttsSpeaking = false;
        resumeListening();
    });
    utterance.addEventListener('error', () => {
        ttsSpeaking = false;
        resumeListening();
    });

    window.speechSynthesis.speak(utterance);
}

function stopEverything() {
    audioQueue = [];

    if (currentAudio) {
        try {
            currentAudio.pause();
            currentAudio.currentTime = 0;
        } catch (error) {
            console.debug('[E.V.] Could not stop current audio:', error);
        }
        currentAudio = null;
    }

    try {
        if ('speechSynthesis' in window) window.speechSynthesis.cancel();
    } catch (error) {
        console.debug('[E.V.] Could not stop browser speech:', error);
    }

    isPlaying = false;
    ttsSpeaking = false;
    currentEvDiv = null;
    setAwaiting(false);
    logSys(T('stopped'));
    resumeListening();
}

// ---------------------------------------------------------------------------
// Microphone + local Whisper path
// ---------------------------------------------------------------------------

let micStream = null;
let audioCtx = null;
let analyser = null;
let mediaRecorder = null;
let recChunks = [];
let recording = false;
let recStartTime = 0;
let lastVoiceTime = 0;
let vadTimer = null;

const START_THRESHOLD = 0.028;
const KEEP_THRESHOLD = 0.020;
const SILENCE_MS = 1800;
const MIN_RECORD_MS = 400;

function mediaSecureEnough() {
    return window.isSecureContext || ['localhost', '127.0.0.1', '::1'].includes(window.location.hostname);
}

async function initMic() {
    if (micStream && analyser && mediaRecorder) return true;

    if (!mediaSecureEnough()) {
        statusEl.textContent = T('micSecure');
        logSys(T('micSecure'));
        return false;
    }

    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        statusEl.textContent = T('micPerm');
        return false;
    }

    statusEl.textContent = T('micPrep');

    try {
        micStream = await navigator.mediaDevices.getUserMedia({
            audio: {
                echoCancellation: true,
                noiseSuppression: true,
                autoGainControl: true,
            },
        });
    } catch (error) {
        console.error('[E.V.] Microphone error:', error);
        statusEl.textContent = T('micPerm');
        return false;
    }

    const Ctx = window.AudioContext || window.webkitAudioContext;
    audioCtx = new Ctx();
    if (audioCtx.state === 'suspended') {
        try {
            await audioCtx.resume();
        } catch (error) {
            console.debug('[E.V.] AudioContext resume:', error);
        }
    }

    const source = audioCtx.createMediaStreamSource(micStream);
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 512;
    source.connect(analyser);

    let mimeType = 'audio/webm;codecs=opus';
    if (!MediaRecorder.isTypeSupported(mimeType)) mimeType = 'audio/webm';
    if (!MediaRecorder.isTypeSupported(mimeType)) mimeType = '';

    mediaRecorder = new MediaRecorder(
        micStream,
        mimeType ? { mimeType } : undefined,
    );

    mediaRecorder.addEventListener('dataavailable', (event) => {
        if (event.data && event.data.size > 0) recChunks.push(event.data);
    });

    mediaRecorder.addEventListener('stop', onRecordingStopped);

    if (!vadTimer) vadTimer = setInterval(vadTick, 50);
    return true;
}

function currentVolume() {
    if (!analyser) return 0;
    const buf = new Uint8Array(analyser.fftSize);
    analyser.getByteTimeDomainData(buf);

    let sum = 0;
    for (const sample of buf) {
        const value = (sample - 128) / 128;
        sum += value * value;
    }
    return Math.sqrt(sum / buf.length);
}

function vadTick() {
    if (!analyser || !mediaRecorder) return;

    if (busy() || !listeningEnabled) {
        if (recording) {
            recording = false;
            try {
                if (mediaRecorder.state === 'recording') mediaRecorder.stop();
            } catch (error) {
                console.debug('[E.V.] Recorder stop:', error);
            }
        }
        return;
    }

    const volume = currentVolume();
    const now = Date.now();

    if (!recording) {
        if (now - lastActivity > IDLE_MUTE_MS) {
            listeningEnabled = false;
            setOrbState('muted');
            statusEl.textContent = T('idleMuted');
            logSys(T('idleLog'));
            return;
        }

        if (volume > START_THRESHOLD) {
            recChunks = [];
            recording = true;
            recStartTime = now;
            lastVoiceTime = now;

            try {
                mediaRecorder.start();
                setOrbState('listening');
            } catch (error) {
                console.error('[E.V.] Recorder start error:', error);
                recording = false;
            }
        }
        return;
    }

    if (volume > KEEP_THRESHOLD) lastVoiceTime = now;

    if (now - lastVoiceTime > SILENCE_MS) {
        recording = false;
        try {
            if (mediaRecorder.state === 'recording') mediaRecorder.stop();
        } catch (error) {
            console.debug('[E.V.] Recorder stop error:', error);
        }
    }
}

function onRecordingStopped() {
    const durationOk = Date.now() - recStartTime >= MIN_RECORD_MS;
    const blob = new Blob(recChunks, { type: 'audio/webm' });
    recChunks = [];

    if (!durationOk || blob.size < 1200) {
        resumeListening();
        return;
    }

    if (!socketOpen()) {
        statusEl.textContent = T('noSocket');
        resumeListening();
        return;
    }

    setOrbState('thinking');
    statusEl.textContent = T('thinking');
    setAwaiting(true);
    markActivity();

    const reader = new FileReader();
    reader.addEventListener('loadend', () => {
        const result = String(reader.result || '');
        const comma = result.indexOf(',');
        const base64Audio = comma >= 0 ? result.slice(comma + 1) : '';

        if (base64Audio && socketOpen()) {
            ws.send(JSON.stringify({ audio: base64Audio }));
        } else {
            setAwaiting(false);
            resumeListening();
        }
    });
    reader.readAsDataURL(blob);
}

async function toggleListen() {
    await unlockAudio();

    const ready = await initMic();
    if (!ready) return;

    listeningEnabled = !listeningEnabled;

    if (listeningEnabled) {
        markActivity();
        if (!busy()) {
            setOrbState('listening');
            statusEl.textContent = T('listening');
        }
    } else {
        if (recording) {
            recording = false;
            try {
                if (mediaRecorder && mediaRecorder.state === 'recording') mediaRecorder.stop();
            } catch (error) {
                console.debug('[E.V.] Recorder stop:', error);
            }
        }
        setOrbState('muted');
        statusEl.textContent = T('muted');
    }
}

function resumeListening() {
    if (busy()) return;

    if (listeningEnabled && analyser) {
        setOrbState('listening');
        statusEl.textContent = T('listening');
    } else {
        setOrbState(analyser ? 'muted' : 'idle');
        statusEl.textContent = analyser ? T('muted') : T('connected');
    }
}

// ---------------------------------------------------------------------------
// Browser-side screen sharing -> Ollama vision
// ---------------------------------------------------------------------------

let visionStream = null;
let visionVideo = null;

function setVisionButton(active) {
    const button = document.getElementById('vision-btn');
    if (!button) return;
    button.textContent = active ? 'VISION ON' : 'VISION';
    button.classList.toggle('on', active);
}

function stopVision() {
    if (visionStream) {
        for (const track of visionStream.getTracks()) track.stop();
    }
    visionStream = null;

    if (visionVideo) {
        visionVideo.srcObject = null;
        visionVideo.remove();
    }
    visionVideo = null;
    setVisionButton(false);
}

async function enableVision() {
    await unlockAudio();

    if (visionStream) {
        stopVision();
        logSys(T('visionOff'));
        return;
    }

    if (!mediaSecureEnough()) {
        statusEl.textContent = T('visionSecure');
        logSys(T('visionSecure'));
        return;
    }

    if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
        statusEl.textContent = T('visionSecure');
        return;
    }

    try {
        visionStream = await navigator.mediaDevices.getDisplayMedia({
            video: {
                frameRate: { ideal: 5, max: 10 },
            },
            audio: false,
        });

        visionVideo = document.createElement('video');
        visionVideo.muted = true;
        visionVideo.playsInline = true;
        visionVideo.srcObject = visionStream;
        visionVideo.style.display = 'none';
        document.body.appendChild(visionVideo);
        await visionVideo.play();

        const track = visionStream.getVideoTracks()[0];
        if (track) {
            track.addEventListener('ended', () => {
                stopVision();
                logSys(T('visionOff'));
            });
        }

        setVisionButton(true);
        logSys(T('visionOn'));
    } catch (error) {
        console.warn('[E.V.] Screen sharing error:', error);
        stopVision();
        statusEl.textContent = T('visionDenied');
    }
}

async function sendVisionFrame() {
    if (!socketOpen()) return;

    const track = visionStream && visionStream.getVideoTracks()[0];
    if (!visionStream || !visionVideo || !track || track.readyState !== 'live') {
        ws.send(JSON.stringify({ screen_error: 'Screen sharing is not enabled.' }));
        setAwaiting(false);
        return;
    }

    const sourceWidth = visionVideo.videoWidth;
    const sourceHeight = visionVideo.videoHeight;

    if (!sourceWidth || !sourceHeight) {
        ws.send(JSON.stringify({ screen_error: 'Screen image is not ready.' }));
        setAwaiting(false);
        return;
    }

    const maxWidth = 1600;
    const scale = Math.min(1, maxWidth / sourceWidth);

    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(sourceWidth * scale));
    canvas.height = Math.max(1, Math.round(sourceHeight * scale));

    const ctx = canvas.getContext('2d', { alpha: false });
    if (!ctx) {
        ws.send(JSON.stringify({ screen_error: 'Could not capture screen frame.' }));
        setAwaiting(false);
        return;
    }

    ctx.drawImage(visionVideo, 0, 0, canvas.width, canvas.height);

    const dataUrl = canvas.toDataURL('image/jpeg', 0.8);
    const comma = dataUrl.indexOf(',');
    const base64Image = comma >= 0 ? dataUrl.slice(comma + 1) : '';

    if (!base64Image) {
        ws.send(JSON.stringify({ screen_error: 'Could not encode screen frame.' }));
        setAwaiting(false);
        return;
    }

    ws.send(JSON.stringify({ screen_image: base64Image }));
}

// ---------------------------------------------------------------------------
// Electron compatibility
// ---------------------------------------------------------------------------

const isElectron = Boolean(window.electronAPI && window.electronAPI.isElectron);
let hiddenByFullscreen = false;

if (isElectron) document.body.classList.add('electron');

function handleFullscreen(active) {
    if (!isElectron || !window.electronAPI) return;

    if (active) {
        if (listeningEnabled) {
            listeningEnabled = false;
            setOrbState('muted');
        }
        if (window.electronAPI.hide) window.electronAPI.hide();
        hiddenByFullscreen = true;
        logSys(T('fsIn'));
    } else if (hiddenByFullscreen) {
        if (window.electronAPI.show) window.electronAPI.show();
        hiddenByFullscreen = false;
        logSys(T('fsOut'));
    }
}

const startCompact = new URLSearchParams(window.location.search).has('hud');

function setMode(mode) {
    document.body.classList.toggle('compact', mode === 'compact');
    document.body.classList.toggle('dashboard', mode === 'dashboard');

    if (isElectron && window.electronAPI.setMode) {
        window.electronAPI.setMode(mode);
    }

    window.setTimeout(resizeViz, 50);
}

setMode(startCompact ? 'compact' : 'dashboard');

// ---------------------------------------------------------------------------
// Stats / clock
// ---------------------------------------------------------------------------

function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value == null ? '—' : String(value);
}

function setBar(id, value) {
    const numeric = Number(value);
    const pct = Number.isFinite(numeric) ? Math.max(0, Math.min(100, numeric)) : 0;
    const bar = document.getElementById(`bar-${id}`);
    const label = document.getElementById(`val-${id}`);

    if (bar) bar.style.width = `${pct}%`;
    if (label) label.textContent = Number.isFinite(numeric) ? `${Math.round(numeric)}%` : '—';
}

async function pollStats() {
    try {
        const response = await fetch('/stats', { cache: 'no-store' });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const stats = await response.json();

        if (stats.language && !langOverride) {
            appLang = stats.language === 'tr' ? 'tr' : 'en';
        }

        if (!i18nApplied) {
            applyI18n();
            i18nApplied = true;
        }

        setBar('cpu', stats.cpu);
        setBar('ram', stats.ram);
        setBar('disk', stats.disk);

        setText('s-ram', `${stats.ram_used_gb ?? '—'}/${stats.ram_total_gb ?? '—'} GB`);
        setText('s-disk', `${stats.disk_free_gb ?? '—'} GB`);
        setText('w-city', stats.city || '—');

        const diskLabel = document.getElementById('lbl-disk');
        if (diskLabel) {
            const word = appLang === 'tr' ? 'BOŞ' : 'FREE';
            diskLabel.textContent = `${word} (${stats.disk_drive || '—'})`;
        }

        if (stats.modules) {
            setText('m-stt', stats.modules.stt || '—');
            setText('m-brain', stats.modules.brain || '—');
            setText('m-voice', stats.modules.voice || '—');
            setText('m-gpu', stats.modules.gpu || '—');
            serverVoice = stats.modules.voice || 'kokoro';
        }

        setText('m-vision', stats.vision_model || stats.modules?.brain || '—');

        if (stats.weather) {
            setText('w-temp', `${stats.weather.temp}°`);
            setText('w-desc', stats.weather.description || '—');
            setText('s-wind', `${stats.weather.wind_kmh} km/h`);
        } else {
            setText('w-temp', '—');
            setText('w-desc', '—');
            setText('s-wind', '—');
        }
    } catch (error) {
        console.debug('[E.V.] /stats error:', error);
    }
}

function updateClock() {
    const now = new Date();
    const locale = appLang === 'tr' ? 'tr-TR' : 'en-US';
    setText('clock-time', now.toLocaleTimeString(locale, {
        hour: '2-digit',
        minute: '2-digit',
    }));
    setText('clock-date', now.toLocaleDateString(locale, {
        weekday: 'long',
        day: 'numeric',
        month: 'long',
    }));
}

// ---------------------------------------------------------------------------
// Orb / visualizer
// ---------------------------------------------------------------------------

const STATUS_LABEL = {
    en: {
        idle: 'ONLINE',
        muted: 'MUTED',
        listening: 'LISTENING',
        thinking: 'PROCESSING',
        speaking: 'SPEAKING',
    },
    tr: {
        idle: 'ONLINE',
        muted: 'SESSİZ',
        listening: 'DİNLİYOR',
        thinking: 'İŞLİYOR',
        speaking: 'KONUŞUYOR',
    },
};

function setOrbState(state) {
    orbState = state;
    const coreState = document.getElementById('core-state');
    const labels = STATUS_LABEL[appLang] || STATUS_LABEL.en;

    if (coreState) coreState.textContent = labels[state] || 'ONLINE';

    const mic = document.getElementById('mic-btn');
    if (mic) mic.classList.toggle('on', state === 'listening');
}

const vctx = vizCanvas.getContext('2d');
let outCtx = null;
let outAnalyser = null;

function hookOutputAnalyser(audioElement) {
    try {
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (!Ctx) return;

        if (!outCtx) {
            outCtx = new Ctx();
            outAnalyser = outCtx.createAnalyser();
            outAnalyser.fftSize = 1024;
            outAnalyser.connect(outCtx.destination);
        }

        if (outCtx.state === 'suspended') outCtx.resume().catch(() => {});
        const source = outCtx.createMediaElementSource(audioElement);
        source.connect(outAnalyser);
    } catch (error) {
        console.debug('[E.V.] Output visualizer unavailable:', error);
    }
}

function resizeViz() {
    const rect = vizCanvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    vizCanvas.width = Math.max(1, Math.round(rect.width * dpr));
    vizCanvas.height = Math.max(1, Math.round(rect.height * dpr));
}

const VIZ_COLOR = {
    idle: '#00e5ff',
    muted: '#5a7183',
    listening: '#00ff66',
    thinking: '#ffb020',
    speaking: '#00e5ff',
};

let vizT = 0;

function drawViz() {
    window.requestAnimationFrame(drawViz);
    if (!vctx) return;
    if (vizCanvas.width < 2 || vizCanvas.height < 2) resizeViz();

    const width = vizCanvas.width;
    const height = vizCanvas.height;
    const cx = width / 2;
    const cy = height / 2;
    const color = VIZ_COLOR[orbState] || VIZ_COLOR.idle;
    const radius = Math.min(width, height) * 0.28;

    vctx.clearRect(0, 0, width, height);

    let data = null;
    if (orbState === 'listening' && analyser) {
        data = new Uint8Array(analyser.fftSize);
        analyser.getByteTimeDomainData(data);
    } else if (orbState === 'speaking' && outAnalyser) {
        data = new Uint8Array(outAnalyser.fftSize);
        outAnalyser.getByteTimeDomainData(data);
    }

    vctx.strokeStyle = 'rgba(0,229,255,0.12)';
    vctx.lineWidth = 1;
    for (const multiplier of [1.55, 1.85]) {
        vctx.beginPath();
        vctx.arc(cx, cy, radius * multiplier, 0, Math.PI * 2);
        vctx.stroke();
    }

    vctx.beginPath();
    vctx.lineWidth = Math.max(1.5, width * 0.0035);
    vctx.strokeStyle = color;
    vctx.shadowBlur = 16;
    vctx.shadowColor = color;

    const samples = 160;
    for (let i = 0; i <= samples; i += 1) {
        const angle = (i / samples) * Math.PI * 2 - Math.PI / 2;
        let amplitude;

        if (data) {
            const index = Math.min(data.length - 1, Math.floor((i / samples) * data.length));
            amplitude = (data[index] - 128) / 128;
        } else {
            const gain = orbState === 'thinking' ? 0.14 : orbState === 'muted' ? 0.03 : 0.07;
            amplitude =
                Math.sin(i * 0.4 + vizT) * gain +
                Math.sin(i * 0.13 - vizT * 1.7) * gain * 0.5;
        }

        const r = radius + amplitude * radius * 0.7;
        const x = cx + Math.cos(angle) * r;
        const y = cy + Math.sin(angle) * r;

        if (i === 0) vctx.moveTo(x, y);
        else vctx.lineTo(x, y);
    }

    vctx.closePath();
    vctx.stroke();

    vctx.shadowBlur = 30;
    vctx.fillStyle = `${color}22`;
    vctx.beginPath();
    vctx.arc(cx, cy, radius * 0.55, 0, Math.PI * 2);
    vctx.fill();
    vctx.shadowBlur = 0;

    vizT += orbState === 'speaking' ? 0.22 : orbState === 'thinking' ? 0.16 : 0.04;
}

// ---------------------------------------------------------------------------
// Decorative workshop / code strip
// ---------------------------------------------------------------------------

function initCodeStrip() {
    const el = document.getElementById('code-line');
    if (!el) return;

    const bits = [
        'EV.core.tick()',
        'stt=whisper.ok',
        'brain=ollama.local',
        'tts=kokoro.local',
        'vision=ollama.local',
        'ws=/ws',
        'audio.sync',
        'core.stable',
        'privacy=local-ai',
    ];

    let text = '';
    for (let i = 0; i < 24; i += 1) {
        text += `${bits[Math.floor(Math.random() * bits.length)]}   `;
    }
    el.textContent = text + text;
}

function initWorkshop() {
    const box = document.getElementById('workshop');
    if (!box) return;

    const pools = {
        en: [
            ['ollama', 'local model online'],
            ['kokoro', 'voice engine ready'],
            ['whisper', 'local STT ready'],
            ['vision', 'waiting for shared screen'],
            ['memory', 'persistent storage mounted'],
            ['network', 'proxy link stable'],
            ['workbench', 'all systems nominal'],
            ['coffee', 'still recommended'],
        ],
        tr: [
            ['ollama', 'yerel model çevrimiçi'],
            ['kokoro', 'ses motoru hazır'],
            ['whisper', 'yerel STT hazır'],
            ['vision', 'paylaşılan ekran bekleniyor'],
            ['bellek', 'kalıcı depolama bağlı'],
            ['ağ', 'proxy bağlantısı kararlı'],
            ['atölye', 'sistemler normal'],
            ['kahve', 'hâlâ öneriliyor'],
        ],
    };

    const append = () => {
        const pool = pools[appLang] || pools.en;
        const [tag, text] = pool[Math.floor(Math.random() * pool.length)];
        const locale = appLang === 'tr' ? 'tr-TR' : 'en-US';
        const time = new Date().toLocaleTimeString(locale, {
            hour: '2-digit',
            minute: '2-digit',
        });

        const line = document.createElement('span');
        line.className = 'wl';

        const timeSpan = document.createElement('span');
        timeSpan.className = 'wt';
        timeSpan.textContent = time;

        const tagSpan = document.createElement('span');
        tagSpan.className = 'tag';
        tagSpan.textContent = tag;

        line.appendChild(timeSpan);
        line.appendChild(tagSpan);
        line.appendChild(document.createTextNode(` ›› ${text}`));
        box.appendChild(line);

        while (box.children.length > 30) box.removeChild(box.firstChild);
        box.scrollTop = box.scrollHeight;
    };

    for (let i = 0; i < 6; i += 1) append();
    window.setInterval(append, 4000);
}

// ---------------------------------------------------------------------------
// Controls
// ---------------------------------------------------------------------------

function wire(id, handler) {
    const el = document.getElementById(id);
    if (el) el.addEventListener('click', handler);
}

wire('btn-expand', () => setMode('dashboard'));
wire('btn-compact', () => setMode('compact'));
wire('mic-btn', () => toggleListen());
wire('vision-btn', () => enableVision());
wire('stop-btn', stopEverything);
wire('send-btn', sendText);

const textInput = document.getElementById('text-input');
if (textInput) {
    textInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
            event.preventDefault();
            sendText();
        }
    });
}

vizCanvas.addEventListener('click', () => toggleListen());

// User gesture can unlock server-generated audio without forcing a mic prompt.
document.addEventListener('pointerdown', () => unlockAudio(), { once: true });

document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
        event.preventDefault();
        stopEverything();
        return;
    }

    if (event.ctrlKey && event.code === 'Space') {
        event.preventDefault();
        toggleListen();
    }
});

if (isElectron && window.electronAPI) {
    if (window.electronAPI.onToggleListen) {
        window.electronAPI.onToggleListen(() => toggleListen());
    }
    wire('btn-quit', () => window.electronAPI.quit && window.electronAPI.quit());
    wire('btn-hide', () => window.electronAPI.hide && window.electronAPI.hide());
}

window.addEventListener('resize', resizeViz);
window.addEventListener('beforeunload', () => {
    stopVision();
    if (micStream) {
        for (const track of micStream.getTracks()) track.stop();
    }
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

applyI18n();
logSys(T('started'));
initCodeStrip();
initWorkshop();
resizeViz();
drawViz();
updateClock();
pollStats();
connect();

window.setInterval(updateClock, 1000);
window.setInterval(pollStats, 2500);
