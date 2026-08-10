FROM python:3.12-slim
 
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV DEBIAN_FRONTEND=noninteractive
 
WORKDIR /app
 
COPY requirements.txt .
 
RUN python -m pip install --upgrade pip \
&& pip install --no-cache-dir -r requirements.txt \
&& python -m playwright install --with-deps chromium
 
COPY . .
 
# Fail the Docker build immediately if the Python source is malformed.
RUN python -m py_compile server.py screen_capture.py
 
RUN mkdir -p /data/whisper
 
EXPOSE 8340
 
CMD ["python", "server.py"]
