"""
E.V. local screen vision through Ollama.
The browser captures the user's chosen screen/window/tab and sends the image.
"""import base64 import httpxasync def describe_image(
    image_bytes: bytes,
    ollama_url: str,
    model: str,
    lang: str = "en",
) -> str:

    if not image_bytes:
        return "No screen image was received."encoded = base64.b64encode(image_bytes).decode("utf-8")

    if lang == "tr":
        prompt = (
            "Bu ekran görüntüsünü dikkatlice incele. ""Ekranda görünen en önemli programları, metinleri ve içeriği ""kısaca açıkla. En fazla 2-3 cümle kullan."        )
    else:
        prompt = (
            "Carefully inspect this screenshot. ""Briefly describe the most important programs, text, UI and ""content visible on the screen. Use at most 2-3 sentences."        )

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

    return response.json()["message"]["content"].strip()
