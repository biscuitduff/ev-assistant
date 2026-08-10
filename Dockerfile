FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1ENV PYTHONUNBUFFERED=1ENV PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium
COPY . .
RUN mkdir -p /data/whisper
EXPOSE 8340CMD ["python", "server.py"]
