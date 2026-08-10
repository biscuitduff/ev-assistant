"""
E.V. — Voice AI Server, Docker/local-AI edition.

Based on Ouru77/ev-assistant's FastAPI backend, adapted for:
- Docker / Portainer configuration through EV_CONFIG_JSON
- local Ollama text generation
- local Ollama screen vision from a browser-shared frame
- local Kokoro TTS
- local faster-whisper STT
- reverse proxies / Cloudflare by binding to 0.0.0.0

Upstream project: https://github.com/Ouru77/ev-assistant
License: MIT
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from typing import Any

import anthropic
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import browser_tools
import command_router
import pc_control
import strings
import whisper_stt


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("EV_CONFIG_PATH", os.path.join(BASE_DIR, "config.json"))
_CONFIG_JSON = os.environ.get("EV_CONFIG_JSON", "").strip()

if _CONFIG_JSON:
    try:
        config = json.loads(_CONFIG_JSON)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"EV_CONFIG_JSON is not valid JSON: {exc}") from exc
else:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

LLM_PROVIDER = str(config.get("llm_provider", "anthropic")).lower()
OLLAMA_URL = str(config.get("ollama_url", "http://localhost:11434")).rstrip("/")
OLLAMA_MODEL = str(config.get("ollama_model", "qwen2.5:7b"))
VISION_MODEL = str(config.get("vision_model", OLLAMA_MODEL))

TTS_PROVIDER = str(config.get("tts_provider", "elevenlabs")).lower()
KOKORO_URL = str(config.get("kokoro_url", "http://kokoro:8880")).rstrip("/")
KOKORO_VOICE = str(config.get("kokoro_voice", "af_heart"))
KOKORO_SPEED = float(config.get("kokoro_speed", 1.0))

STT_PROVIDER = str(config.get("stt_provider", "browser")).lower()
WHISPER_MODEL = str(config.get("whisper_model", "small"))
WHISPER_DIR = str(config.get("whisper_dir", ""))

ANTHROPIC_API_KEY = str(config.get("anthropic_api_key", ""))
ELEVENLABS_API_KEY = str(config.get("elevenlabs_api_key", ""))
ELEVENLABS_VOICE_ID = str(
    config.get("elevenlabs_voice_id", "rDmv3mOhK6TnhYWckFaD")
)

USER_NAME = str(config.get("user_name", "dostum"))
USER_ADDRESS = str(config.get("user_address", "dostum"))
CITY = str(config.get("city", "London"))
LANGUAGE = str(config.get("language", "tr")).lower()
TASKS_FILE = str(config.get("obsidian_inbox_path", ""))

PC_CONTROL = bool(config.get("pc_control", True))
CONVERSATION_MODE = bool(config.get("conversation_mode", True))
HISTORY_TURNS = int(config.get("history_turns", 30))
NUM_CTX = int(config.get("num_ctx", 4096))

MEMORY_PATH = str(config.get("memory_path", os.path.join(BASE_DIR, "memory.json")))


# ---------------------------------------------------------------------------
# App / shared clients
# ---------------------------------------------------------------------------

app = FastAPI()
http = httpx.AsyncClient(timeout=180)

conversations: dict[str, list[dict[str, str]]] = {}
pending_actions: dict[str, dict[str, str]] = {}
clients: set[WebSocket] = set()

pc_control.set_language(LANGUAGE)

if STT_PROVIDER == "whisper":
    whisper_stt.configure(WHISPER_MODEL, WHISPER_DIR or None, LANGUAGE)


# ---------------------------------------------------------------------------
# Localized strings
# ---------------------------------------------------------------------------


def S(key: str, **kw: Any) -> str:
    """Localized string helper with USER_ADDRESS as the default name."""
    kw.setdefault("name", USER_ADDRESS)
    return strings.s(key, LANGUAGE, **kw)


# ---------------------------------------------------------------------------
# Long-term memory
# ---------------------------------------------------------------------------


def load_memories() -> list[str]:
    try:
        with open(MEMORY_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        facts = data.get("facts", [])
        return facts if isinstance(facts, list) else []
    except Exception:
        return []


def save_memories(facts: list[str]) -> None:
    try:
        parent = os.path.dirname(MEMORY_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(MEMORY_PATH, "w", encoding="utf-8") as f:
            json.dump({"facts": facts}, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"[E.V.] Failed to write memory: {exc}", flush=True)


def add_memory(fact: str) -> str:
    fact = (fact or "").strip().rstrip(".")
    if not fact:
        return ""

    facts = load_memories()
    if any(fact.lower() == str(existing).lower() for existing in facts):
        return fact

    facts.append(fact)
    save_memories(facts[-50:])
    return fact


def forget_memory(query: str) -> bool:
    query = (query or "").strip().lower()
    facts = load_memories()

    if query in ("hepsi", "her şey", "hepsini", "all", "everything"):
        save_memories([])
        return True

    kept = [f for f in facts if query not in str(f).lower()]
    if len(kept) != len(facts):
        save_memories(kept)
        return True
    return False


MEMORIES = load_memories()


# ---------------------------------------------------------------------------
# Optional Anthropic client; local Docker deployment normally uses Ollama.
# ---------------------------------------------------------------------------

ai = None
if ANTHROPIC_API_KEY and "YAPISTIR" not in ANTHROPIC_API_KEY:
    try:
        ai = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
    except Exception as exc:
        print(f"[E.V.] Anthropic client init failed: {exc}", flush=True)


# ---------------------------------------------------------------------------
# LLM backends
# ---------------------------------------------------------------------------


async def llm_chat(system: str, messages: list, max_tokens: int = 400) -> str:
    """Send one non-streaming request to the configured LLM backend."""
    if LLM_PROVIDER == "ollama":
        msgs = [{"role": "system", "content": system}] + messages
        last_err: Exception | None = None

        for ctx in (NUM_CTX, None):
            options: dict[str, Any] = {
                "num_predict": max_tokens,
                "temperature": 0.7,
            }
            if ctx:
                options["num_ctx"] = ctx

            payload = {
                "model": OLLAMA_MODEL,
                "messages": msgs,
                "stream": False,
                "keep_alive": "30m",
                "options": options,
            }

            try:
                response = await http.post(f"{OLLAMA_URL}/api/chat", json=payload)
                response.raise_for_status()
                return str(response.json()["message"]["content"])
            except Exception as exc:
                last_err = exc
                print(
                    f"[E.V.] llm_chat ctx={ctx} failed, falling back: {str(exc)[:120]}",
                    flush=True,
                )

        if last_err:
            raise last_err
        raise RuntimeError("Ollama request failed without an exception")

    if ai is None:
        return S("no_anthropic")

    response = await ai.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    return response.content[0].text


async def llm_stream(system: str, messages: list, max_tokens: int = 400):
    """Yield incremental text chunks. Ollama is streamed; Anthropic is not."""
    if LLM_PROVIDER != "ollama":
        yield await llm_chat(system, messages, max_tokens)
        return

    msgs = [{"role": "system", "content": system}] + messages
    last_err: Exception | None = None

    for ctx in (NUM_CTX, None):
        options: dict[str, Any] = {
            "num_predict": max_tokens,
            "temperature": 0.7,
        }
        if ctx:
            options["num_ctx"] = ctx

        payload = {
            "model": OLLAMA_MODEL,
            "messages": msgs,
            "stream": True,
            "keep_alive": "30m",
            "options": options,
        }

        yielded = False
        try:
            async with http.stream(
                "POST",
                f"{OLLAMA_URL}/api/chat",
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue

                    piece = obj.get("message", {}).get("content", "")
                    if piece:
                        yielded = True
                        yield piece

                    if obj.get("done"):
                        break
            return
        except Exception as exc:
            last_err = exc
            print(
                f"[E.V.] llm_stream ctx={ctx} failed, falling back: {str(exc)[:120]}",
                flush=True,
            )
            if yielded:
                raise

    if last_err:
        raise last_err


# ---------------------------------------------------------------------------
# Weather / tasks
# ---------------------------------------------------------------------------

WMO_TR = {
    0: "açık",
    1: "az bulutlu",
    2: "parçalı bulutlu",
    3: "çok bulutlu",
    45: "sisli",
    48: "kırağılı sis",
    51: "hafif çisenti",
    53: "çisenti",
    55: "yoğun çisenti",
    61: "hafif yağmur",
    63: "yağmurlu",
    65: "kuvvetli yağmur",
    71: "hafif kar",
    73: "karlı",
    75: "yoğun kar",
    77: "kar taneleri",
    80: "sağanak",
    81: "sağanak yağış",
    82: "şiddetli sağanak",
    95: "gök gürültülü fırtına",
    96: "dolulu fırtına",
    99: "şiddetli dolulu fırtına",
}

WMO_EN = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "rime fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "showers",
    81: "rain showers",
    82: "heavy showers",
    95: "thunderstorm",
    96: "hail storm",
    99: "severe hail storm",
}


def get_weather_sync():
    """Fetch current weather from Open-Meteo. Returns None if unavailable."""
    import urllib.parse
    import urllib.request

    try:
        q = urllib.parse.quote(CITY)
        geo_url = (
            "https://geocoding-api.open-meteo.com/v1/search"
            f"?name={q}&count=1&language={LANGUAGE if LANGUAGE in ('en', 'tr') else 'en'}"
        )
        geo = json.loads(urllib.request.urlopen(geo_url, timeout=6).read())
        results = geo.get("results") or []
        if not results:
            return None

        loc = results[0]
        lat = loc["latitude"]
        lon = loc["longitude"]
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&current=temperature_2m,apparent_temperature,relative_humidity_2m,"
            "weather_code,wind_speed_10m"
        )
        current = json.loads(urllib.request.urlopen(url, timeout=6).read())["current"]
        wmo = WMO_EN if LANGUAGE == "en" else WMO_TR
        return {
            "temp": round(current["temperature_2m"]),
            "feels_like": round(current["apparent_temperature"]),
            "description": wmo.get(current["weather_code"], "—"),
            "humidity": current["relative_humidity_2m"],
            "wind_kmh": round(current["wind_speed_10m"]),
        }
    except Exception as exc:
        print(f"[E.V.] Weather unavailable: {exc}", flush=True)
        return None


def get_tasks_sync() -> list[str]:
    """Read unchecked tasks from Obsidian's Tasks.md when configured."""
    if not TASKS_FILE:
        return []

    try:
        tasks_path = os.path.join(TASKS_FILE, "Tasks.md")
        with open(tasks_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return [
            line.strip().replace("- [ ]", "").strip()
            for line in lines
            if line.strip().startswith("- [ ]")
        ]
    except Exception:
        return []


def refresh_data() -> None:
    global WEATHER_INFO, TASKS_INFO
    WEATHER_INFO = get_weather_sync()
    TASKS_INFO = get_tasks_sync()
    print(f"[E.V.] Weather: {WEATHER_INFO}", flush=True)
    print(f"[E.V.] Tasks: {len(TASKS_INFO)} loaded", flush=True)


WEATHER_INFO = None
TASKS_INFO: list[str] = []
refresh_data()


# ---------------------------------------------------------------------------
# Prompt / action parsing
# ---------------------------------------------------------------------------

ACTION_PATTERN = re.compile(r"\[ACTION:(\w+)\]\s*(.*?)$", re.DOTALL | re.MULTILINE)


def build_system_prompt() -> str:
    wb = strings.weather_block(LANGUAGE, CITY, WEATHER_INFO) if WEATHER_INFO else ""
    tb = strings.task_block(LANGUAGE, TASKS_INFO) if TASKS_INFO else ""
    memories = load_memories()
    mb = strings.memory_block(LANGUAGE, USER_NAME, memories) if memories else ""
    pb = strings.pc_block(LANGUAGE) if PC_CONTROL else ""
    return strings.system_prompt(
        LANGUAGE,
        USER_NAME,
        USER_ADDRESS,
        CITY,
        wb,
        tb,
        mb,
        pb,
    )


def greeting_prefix() -> str:
    return strings.greeting_prefix(LANGUAGE, time.localtime().tm_hour)


def build_greeting() -> str:
    return " ".join(
        [
            f"{greeting_prefix()} {USER_NAME},",
            strings.s("greet_ready", LANGUAGE),
        ]
    )


def get_system_prompt() -> str:
    return (
        build_system_prompt()
        .replace("{time}", time.strftime("%H:%M"))
        .replace("{greeting}", greeting_prefix())
    )


def extract_action(text: str):
    match = ACTION_PATTERN.search(text or "")
    if match:
        clean = text[: match.start()].strip()
        return clean, {
            "type": match.group(1).strip().upper(),
            "payload": match.group(2).strip(),
        }
    return text, None


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------


async def synthesize_speech(text: str) -> bytes:
    if not (text or "").strip():
        return b""

    # Browser TTS is generated client-side.
    if TTS_PROVIDER == "browser":
        return b""

    text = re.sub(r"E\.V\.?", "İvi" if LANGUAGE != "en" else "Evie", text)

    # Fully local Kokoro TTS.
    if TTS_PROVIDER == "kokoro":
        try:
            response = await http.post(
                f"{KOKORO_URL}/v1/audio/speech",
                json={
                    "model": "kokoro",
                    "input": text,
                    "voice": KOKORO_VOICE,
                    "response_format": "mp3",
                    "speed": KOKORO_SPEED,
                },
                timeout=180,
            )
            response.raise_for_status()
            return response.content
        except Exception as exc:
            print(f"[E.V.] Kokoro TTS error: {exc}", flush=True)
            return b""

    # Upstream ElevenLabs path retained for non-Docker users.
    chunks: list[str] = []
    if len(text) > 250:
        sentences = re.split(r"(?<=[.!?])\s+", text)
        current = ""
        for sentence in sentences:
            if len(current) + len(sentence) > 250 and current:
                chunks.append(current.strip())
                current = sentence
            else:
                current = (current + " " + sentence).strip()
        if current:
            chunks.append(current.strip())
    else:
        chunks = [text]

    audio_parts: list[bytes] = []
    for chunk in chunks:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
        try:
            response = await http.post(
                url,
                headers={
                    "xi-api-key": ELEVENLABS_API_KEY,
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg",
                },
                json={
                    "text": chunk,
                    "model_id": "eleven_turbo_v2_5",
                    "voice_settings": {
                        "stability": 0.5,
                        "similarity_boost": 0.85,
                    },
                },
            )
            print(
                f"[E.V.] TTS chunk status: {response.status_code}, size: {len(response.content)}",
                flush=True,
            )
            if response.status_code == 200:
                audio_parts.append(response.content)
            else:
                print(f"[E.V.] TTS error body: {response.text[:200]}", flush=True)
        except Exception as exc:
            print(f"[E.V.] TTS exception: {exc}", flush=True)

    return b"".join(audio_parts)


# ---------------------------------------------------------------------------
# Local screen vision
# ---------------------------------------------------------------------------


async def describe_screen_image(image_bytes: bytes) -> str:
    """Describe one browser-captured JPEG/PNG frame with local Ollama vision."""
    if not image_bytes:
        return (
            "No screen image was received."
            if LANGUAGE == "en"
            else "Ekran görüntüsü alınamadı."
        )

    encoded = base64.b64encode(image_bytes).decode("utf-8")

    if LANGUAGE == "tr":
        prompt = (
            "Bu ekran görüntüsünü dikkatlice incele. Ekranda görünen en önemli "
            "programları, okunabilir metinleri, arayüz öğelerini ve içeriği kısaca "
            "açıkla. En fazla 2-3 cümle kullan. Emin olmadığın şeyi uydurma."
        )
    else:
        prompt = (
            "Carefully inspect this screenshot. Briefly describe the most important "
            "programs, readable text, UI elements, and content visible on screen. "
            "Use at most 2-3 sentences and do not guess when uncertain."
        )

    payload = {
        "model": VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [encoded],
            }
        ],
        "stream": False,
        "keep_alive": "10m",
        "options": {
            "temperature": 0.2,
            "num_predict": 300,
        },
    }

    response = await http.post(
        f"{OLLAMA_URL}/api/chat",
        json=payload,
        timeout=180,
    )
    response.raise_for_status()
    result = response.json()
    text = str(result.get("message", {}).get("content", "")).strip()

    if text:
        return text
    return "I couldn't make out the screen." if LANGUAGE == "en" else "Ekranı anlayamadım."


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


async def execute_action(action: dict[str, str]) -> str:
    t = action.get("type", "").upper()
    p = action.get("payload", "")

    if t == "SEARCH":
        result = await browser_tools.search_and_read(p)
        if "error" not in result:
            return (
                f"Page: {result.get('title', '')}\n"
                f"URL: {result.get('url', '')}\n\n"
                f"{result.get('content', '')[:2000]}"
            )
        return f"__FAILED__Search failed: {result.get('error', '')}"

    if t == "BROWSE":
        result = await browser_tools.visit(p)
        if "error" not in result:
            return f"Page: {result.get('title', '')}\n\n{result.get('content', '')[:2000]}"
        return f"__FAILED__Couldn't reach the page: {result.get('error', '')}"

    if t == "OPEN":
        await browser_tools.open_url(p)
        return f"Opened: {p}"

    if t == "SCREEN":
        # Browser-side capture is requested by deliver_action().
        return ""

    if t == "NEWS":
        return await browser_tools.fetch_news()

    if t == "REMEMBER":
        saved = add_memory(p)
        global MEMORIES
        MEMORIES = load_memories()
        return f"__SPOKEN__{S('mem_saved')}" if saved else ""

    if t == "FORGET":
        ok = forget_memory(p)
        MEMORIES = load_memories()
        return "__SPOKEN__" + (S("mem_forgot") if ok else S("mem_none"))

    # Windows-only functions are preserved but normally unreachable in Docker
    # because the supplied stack sets pc_control=false.
    if t == "APP":
        return "__SPOKEN__" + await asyncio.to_thread(pc_control.open_app, p)

    if t == "MEDIA":
        return "__SPOKEN__" + await asyncio.to_thread(pc_control.media, p)

    if t == "CLOSE":
        return "__SPOKEN__" + await asyncio.to_thread(pc_control.close_app, p)

    if t == "POWER":
        return "__SPOKEN__" + await asyncio.to_thread(pc_control.power, p)

    if t == "CMD":
        return await asyncio.to_thread(pc_control.run_command, p)

    if t == "MOUSE":
        return "__SPOKEN__" + await asyncio.to_thread(pc_control.mouse, p)

    if t == "YOUTUBE":
        result = await browser_tools.youtube_open(p)
        if "error" in result:
            return f"__SPOKEN__{S('action_failed')}"
        if LANGUAGE == "en":
            return f"__SPOKEN__Opening {p} on YouTube."
        return f"__SPOKEN__YouTube'da {p} açılıyor."

    return ""


DESTRUCTIVE_ACTIONS = {"CLOSE", "POWER", "CMD"}


def confirm_prompt(action: dict[str, str]) -> str:
    t = action.get("type", "").upper()
    p = action.get("payload", "")

    if t == "CLOSE":
        return S("confirm_close", app=p)
    if t == "POWER":
        verb = strings.power_confirm(p, LANGUAGE)
        return (
            f"{verb}, {USER_ADDRESS}?"
            if LANGUAGE == "en"
            else f"{verb} mı, {USER_ADDRESS}?"
        )
    if t == "CMD":
        return S("confirm_cmd", cmd=p)
    return S("confirm_generic")


# ---------------------------------------------------------------------------
# Startup helpers
# ---------------------------------------------------------------------------


async def _warm_ollama() -> None:
    if LLM_PROVIDER != "ollama":
        return

    try:
        response = await http.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "stream": False,
                "keep_alive": "30m",
                "messages": [{"role": "user", "content": "hello"}],
                "options": {
                    "num_predict": 1,
                    "num_ctx": NUM_CTX,
                },
            },
            timeout=180,
        )
        response.raise_for_status()
        print("[E.V.] Ollama model warmed.", flush=True)
    except Exception as exc:
        # This is expected until the configured model has been pulled.
        print(f"[E.V.] Warm-up error: {exc}", flush=True)


async def _fullscreen_watch() -> None:
    """Windows desktop helper. Disabled when PC_CONTROL is false."""
    last = None
    while True:
        try:
            active = await asyncio.to_thread(pc_control.fullscreen_active)
            if active != last:
                last = active
                dead: list[WebSocket] = []
                for ws in list(clients):
                    try:
                        await ws.send_json({"type": "fullscreen", "active": active})
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    clients.discard(ws)
        except Exception as exc:
            print(f"[E.V.] Fullscreen watch error: {exc}", flush=True)
        await asyncio.sleep(1.5)


@app.on_event("startup")
async def _on_startup() -> None:
    asyncio.create_task(_warm_ollama())
    if PC_CONTROL:
        asyncio.create_task(_fullscreen_watch())


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    try:
        await browser_tools.close()
    except Exception:
        pass
    try:
        await http.aclose()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Conversation processing
# ---------------------------------------------------------------------------


async def speak_chunk(text: str, ws: WebSocket) -> None:
    text = (text or "").strip()
    if not text:
        return

    audio = await synthesize_speech(text)
    print(f"  E.V. »: {text[:80]}", flush=True)
    await ws.send_json(
        {
            "type": "response_chunk",
            "text": text,
            "audio": base64.b64encode(audio).decode("utf-8") if audio else "",
        }
    )


_YES = {
    "evet",
    "olur",
    "tamam",
    "yap",
    "tabii",
    "tabi",
    "aynen",
    "onayla",
    "kesinlikle",
    "elbette",
    "yapabilirsin",
    "devam",
    "hı hı",
    "hıhı",
    "yes",
    "yeah",
    "yep",
    "sure",
    "ok",
    "okay",
    "go ahead",
    "do it",
}

_NO = {
    "hayır",
    "hayir",
    "yok",
    "dur",
    "vazgeç",
    "vazgec",
    "iptal",
    "istemiyorum",
    "gerek yok",
    "boşver",
    "bosver",
    "olmaz",
    "no",
    "nope",
    "don't",
    "dont",
    "cancel",
    "stop",
    "never mind",
    "nevermind",
}


def _matches(text: str, words: set[str]) -> bool:
    normalized = " " + re.sub(r"[^\wçğıöşü ]", " ", (text or "").lower()) + " "
    return any(f" {word} " in normalized for word in words)


async def _speak_response(session_id: str, text: str, ws: WebSocket) -> None:
    audio = await synthesize_speech(text)
    conversations.setdefault(session_id, []).append(
        {"role": "assistant", "content": text}
    )
    await ws.send_json(
        {
            "type": "response",
            "text": text,
            "audio": base64.b64encode(audio).decode("utf-8") if audio else "",
        }
    )


async def process_message(session_id: str, user_text: str, ws: WebSocket) -> None:
    conversations.setdefault(session_id, [])

    # A spoken yes/no can approve or cancel a destructive Windows action.
    if session_id in pending_actions:
        if _matches(user_text, _YES):
            action = pending_actions.pop(session_id)
            await deliver_action(session_id, action, ws)
            return

        if _matches(user_text, _NO):
            pending_actions.pop(session_id, None)
            await _speak_response(session_id, S("canceled"), ws)
            return

        pending_actions.pop(session_id, None)

    lower_text = user_text.lower()
    is_greeting = any(
        token in lower_text
        for token in ("selam", "aktif", "activate", "hello", "hi ", "hey", "merhaba")
    )

    if is_greeting:
        refresh_data()

    conversations[session_id].append({"role": "user", "content": user_text})

    # Clean first greeting without an LLM round trip.
    if len(conversations[session_id]) == 1 and is_greeting:
        greeting = build_greeting()
        audio = await synthesize_speech(greeting)
        conversations[session_id].append(
            {"role": "assistant", "content": greeting}
        )
        print(f"  E.V. (greeting): {greeting}", flush=True)
        await ws.send_json(
            {
                "type": "response",
                "text": greeting,
                "audio": base64.b64encode(audio).decode("utf-8") if audio else "",
            }
        )
        return

    routed = command_router.route(user_text, LANGUAGE)
    if routed and (
        PC_CONTROL or routed["type"] in ("REMEMBER", "FORGET", "SLEEP")
    ):
        print(
            f"  Routed: {routed['type']} -> {routed.get('payload', '')[:80]}",
            flush=True,
        )

        if routed["type"] == "SLEEP":
            await _speak_response(session_id, S("bye"), ws)
            await ws.send_json({"type": "sleep"})
            return

        if routed["type"] in DESTRUCTIVE_ACTIONS:
            pending_actions[session_id] = routed
            question = confirm_prompt(routed)
            audio = await synthesize_speech(question)
            await ws.send_json(
                {
                    "type": "confirm",
                    "text": question,
                    "audio": base64.b64encode(audio).decode("utf-8") if audio else "",
                }
            )
            return

        await deliver_action(session_id, routed, ws)
        return

    history = conversations[session_id][-HISTORY_TURNS:]

    if TTS_PROVIDER == "browser":
        full = await llm_chat(get_system_prompt(), history, max_tokens=400)
        spoken_text, action = extract_action(full)
        if spoken_text:
            conversations[session_id].append(
                {"role": "assistant", "content": spoken_text}
            )
            await ws.send_json(
                {"type": "response", "text": spoken_text, "audio": ""}
            )
    else:
        full = ""
        buf = ""
        stop_speaking = False

        try:
            async for piece in llm_stream(get_system_prompt(), history, 400):
                full += piece

                if stop_speaking:
                    continue

                buf += piece
                if "[ACTION" in buf:
                    action_pos = buf.find("[ACTION")
                    await speak_chunk(buf[:action_pos], ws)
                    buf = ""
                    stop_speaking = True
                    continue

                segments = re.split(r"(?<=[.!?…])\s+", buf)
                if len(segments) > 1:
                    for segment in segments[:-1]:
                        await speak_chunk(segment, ws)
                    buf = segments[-1]
        except Exception as exc:
            print(f"  Stream error: {exc}", flush=True)

        if not stop_speaking:
            await speak_chunk(buf, ws)

        spoken_text, action = extract_action(full)
        if spoken_text:
            conversations[session_id].append(
                {"role": "assistant", "content": spoken_text}
            )
        await ws.send_json({"type": "response_done"})

    print(f"  LLM raw: {full[:200]}", flush=True)

    if action:
        print(
            f"  Action: {action['type']} -> {action.get('payload', '')[:100]}",
            flush=True,
        )

        if action["type"] in DESTRUCTIVE_ACTIONS:
            pending_actions[session_id] = action
            question = confirm_prompt(action)
            audio = await synthesize_speech(question)
            await ws.send_json(
                {
                    "type": "confirm",
                    "text": question,
                    "audio": base64.b64encode(audio).decode("utf-8") if audio else "",
                }
            )
            return

        await deliver_action(session_id, action, ws)


async def deliver_action(
    session_id: str,
    action: dict[str, str],
    ws: WebSocket,
) -> None:
    """Run a non-destructive/already-approved action and speak its result."""

    # Docker screen vision is client-side capture + server-side Ollama inference.
    if action.get("type", "").upper() == "SCREEN":
        hint = S("screen_hint")
        hint_audio = await synthesize_speech(hint)
        await ws.send_json(
            {
                "type": "response",
                "text": hint,
                "audio": (
                    base64.b64encode(hint_audio).decode("utf-8")
                    if hint_audio
                    else ""
                ),
            }
        )
        await ws.send_json({"type": "screen_request"})
        return

    try:
        action_result = await execute_action(action)
        print(f"  Result: {str(action_result)[:200]}", flush=True)
    except Exception as exc:
        print(f"  Action error: {exc}", flush=True)
        action_result = f"__SPOKEN__{S('action_problem')}"

    if action.get("type", "").upper() == "OPEN":
        return

    if not action_result:
        return

    if isinstance(action_result, str) and action_result.startswith("__SPOKEN__"):
        summary = action_result[len("__SPOKEN__") :].strip()
    elif not action_result.startswith("__FAILED__"):
        if LANGUAGE == "en":
            sys_sum = (
                "You are E.V. Summarize the info below briefly in English, at most "
                "3 sentences, calm and plain E.V. style. Address the user by name as "
                f"'{USER_ADDRESS}'. NO brackets/tags. NO ACTION tag."
            )
            user_sum = f"Summarize this:\n\n{action_result}"
        else:
            sys_sum = (
                "Sen E.V.'sin. Aşağıdaki bilgileri KISA şekilde Türkçe özetle, en "
                "fazla 3 cümle, sakin ve sade E.V. tarzında. Kullanıcıya "
                f"'{USER_ADDRESS}' diye ismiyle hitap et. Köşeli parantez içinde "
                "etiket YOK. ACTION etiketi YOK."
            )
            user_sum = f"Şunu özetle:\n\n{action_result}"

        summary = await llm_chat(
            sys_sum,
            [{"role": "user", "content": user_sum}],
            max_tokens=250,
        )
        summary, _ = extract_action(summary)
    else:
        summary = S("action_failed")

    audio = await synthesize_speech(summary)
    conversations.setdefault(session_id, []).append(
        {"role": "assistant", "content": summary}
    )
    await ws.send_json(
        {
            "type": "response",
            "text": summary,
            "audio": base64.b64encode(audio).decode("utf-8") if audio else "",
        }
    )


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    session_id = str(id(ws))
    clients.add(ws)
    print("[E.V.] Client connected", flush=True)

    try:
        while True:
            data = await ws.receive_json()

            try:
                # Browser reported that the user has not enabled screen sharing.
                if data.get("screen_error"):
                    message = (
                        "Screen sharing isn't enabled. Click VISION first and choose a screen or window."
                        if LANGUAGE == "en"
                        else "Ekran paylaşımı açık değil. Önce VISION düğmesine basıp bir ekran veya pencere seç."
                    )
                    await _speak_response(session_id, message, ws)
                    continue

                # Browser-provided screen frame -> local Ollama vision.
                if data.get("screen_image"):
                    try:
                        image_bytes = base64.b64decode(
                            data["screen_image"],
                            validate=True,
                        )
                        if len(image_bytes) > 8 * 1024 * 1024:
                            raise ValueError("screen image exceeds 8 MiB")

                        description = await describe_screen_image(image_bytes)
                        await _speak_response(session_id, description, ws)
                    except Exception as exc:
                        print(f"  Vision error: {exc}", flush=True)
                        message = (
                            "Something went wrong while examining the screen."
                            if LANGUAGE == "en"
                            else "Ekranı incelerken bir sorun çıktı."
                        )
                        await _speak_response(session_id, message, ws)
                    continue

                # Confirmation button/response from the client.
                if "confirm" in data:
                    pending = pending_actions.pop(session_id, None)
                    if data.get("confirm") and pending:
                        await deliver_action(session_id, pending, ws)
                    else:
                        cancel = S("canceled")
                        cancel_audio = await synthesize_speech(cancel)
                        await ws.send_json(
                            {
                                "type": "response",
                                "text": cancel,
                                "audio": (
                                    base64.b64encode(cancel_audio).decode("utf-8")
                                    if cancel_audio
                                    else ""
                                ),
                            }
                        )
                    continue

                # Local Whisper path: browser sends recorded webm/opus as base64.
                if data.get("audio"):
                    try:
                        audio_bytes = base64.b64decode(data["audio"])
                        user_text = await asyncio.to_thread(
                            whisper_stt.transcribe,
                            audio_bytes,
                        )
                    except Exception as exc:
                        print(f"  STT error: {exc}", flush=True)
                        await ws.send_json({"type": "idle"})
                        continue

                    user_text = (user_text or "").strip()
                    if not user_text:
                        await ws.send_json({"type": "idle"})
                        continue

                    await ws.send_json({"type": "user_text", "text": user_text})
                    print(f"  You(whisper): {user_text}", flush=True)
                    await process_message(session_id, user_text, ws)
                    continue

                # Plain text path.
                user_text = str(data.get("text", "")).strip()
                if not user_text:
                    continue

                print(f"  You: {user_text}", flush=True)
                await process_message(session_id, user_text, ws)

            except WebSocketDisconnect:
                raise
            except Exception as exc:
                print(
                    f"  Processing error: {type(exc).__name__}: {exc}",
                    flush=True,
                )
                try:
                    await ws.send_json(
                        {
                            "type": "response",
                            "text": S("glitch"),
                            "audio": "",
                        }
                    )
                except Exception:
                    pass

    except WebSocketDisconnect:
        conversations.pop(session_id, None)
        pending_actions.pop(session_id, None)
    finally:
        clients.discard(ws)


# ---------------------------------------------------------------------------
# Static UI / stats
# ---------------------------------------------------------------------------

app.mount(
    "/static",
    StaticFiles(directory=os.path.join(BASE_DIR, "frontend")),
    name="static",
)

_GPU_NAME: str | None = None


def get_gpu_name() -> str:
    """Best-effort GPU display. CPU-only Linux simply reports CPU."""
    global _GPU_NAME

    if _GPU_NAME is not None:
        return _GPU_NAME

    if os.name != "nt":
        _GPU_NAME = "CPU"
        return _GPU_NAME

    name = "GPU"
    try:
        import subprocess

        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_VideoController | Sort-Object AdapterRAM -Descending "
                "| Select-Object -First 1).Name",
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )
        found = (result.stdout or "").strip()
        if found:
            name = (
                found.replace("AMD ", "")
                .replace("NVIDIA ", "")
                .replace("(R)", "")
                .strip()
            )
    except Exception:
        pass

    _GPU_NAME = name
    return name


def module_info() -> dict[str, str]:
    brain = OLLAMA_MODEL if LLM_PROVIDER == "ollama" else "claude"
    return {
        "stt": STT_PROVIDER,
        "brain": brain,
        "voice": TTS_PROVIDER,
        "gpu": get_gpu_name(),
    }


@app.get("/stats")
async def stats():
    import psutil

    vm = psutil.virtual_memory()
    app_drive = os.path.splitdrive(os.path.abspath(__file__))[0] + os.sep

    try:
        disk = psutil.disk_usage(app_drive)
    except Exception:
        disk = psutil.disk_usage(os.sep)

    return {
        "cpu": round(psutil.cpu_percent(interval=None)),
        "ram": round(vm.percent),
        "ram_used_gb": round(vm.used / 1e9, 1),
        "ram_total_gb": round(vm.total / 1e9, 1),
        "disk": round(disk.percent),
        "disk_free_gb": round(disk.free / 1e9, 0),
        "disk_drive": app_drive.rstrip("\\/"),
        "weather": WEATHER_INFO or None,
        "city": CITY,
        "language": LANGUAGE,
        "modules": module_info(),
        "vision_model": VISION_MODEL,
    }


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(os.path.join(BASE_DIR, "electron", "icon.png"))


@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(BASE_DIR, "frontend", "index.html"))


if __name__ == "__main__":
    import uvicorn

    print("=" * 56, flush=True)
    print("  E.V. — Voice AI Server · Docker/local-AI edition", flush=True)
    print("  Listening on 0.0.0.0:8340", flush=True)
    print(f"  Brain: {OLLAMA_MODEL if LLM_PROVIDER == 'ollama' else LLM_PROVIDER}", flush=True)
    print(f"  Vision: {VISION_MODEL}", flush=True)
    print(f"  Voice: {TTS_PROVIDER}", flush=True)
    print("=" * 56, flush=True)

    # Required for access from Nginx Proxy Manager on another Docker container.
    uvicorn.run(app, host="0.0.0.0", port=8340)
