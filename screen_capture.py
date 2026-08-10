"""
E.V. screen-vision helper for the Docker/local-AI build.

In the Docker edition the user's browser captures the selected screen/window/tab.
The image bytes can be sent to Ollama for local vision analysis.
"""

from __future__ import annotations

import base64

import httpx


async def describe_image(
    image_bytes: bytes,
    ollama_url: str,
    model: str,
    lang: str = "en",
) -> str:
    """Describe a browser-provided screenshot using a local Ollama vision model."""
    if not image_bytes:
        return "No screen image was received."

    encoded = base64.b64encode(image_bytes).decode("utf-8")

    if lang.lower() == "tr":
        prompt = (
            "Bu ekran görüntüsünü dikkatlice incele. "
            "Ekranda görünen en önemli programları, metinleri, arayüz öğelerini "
            "ve içeriği kısaca açıkla. En fazla 2-3 cümle kullan."
        )
    else:
        prompt = (
            "Carefully inspect this screenshot. "
            "Briefly describe the most important programs, text, UI elements, "
            "and content visible on the screen. Use at most 2-3 sentences."
        )

    payload = {
        "model": model,
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

    async with httpx.AsyncClient(timeout=180) as client:
        response = await client.post(
            f"{ollama_url.rstrip('/')}/api/chat",
            json=payload,
        )
        response.raise_for_status()

    data = response.json()
    return data.get("message", {}).get("content", "").strip()


async def describe_screen(_client=None, lang: str = "en") -> str:
    """Compatibility fallback for old callers that expected server-side capture."""
    if lang.lower() == "tr":
        return (
            "Bu Docker sürümünde ekran görüntüsü tarayıcı tarafından paylaşılır. "
            "VISION düğmesini açıp tekrar deneyin."
        )

    return (
        "In this Docker build, screen capture is provided by the browser. "
        "Enable VISION and try again."
    )
