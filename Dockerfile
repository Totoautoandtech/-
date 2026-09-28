FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# FFmpeg inclut ffprobe/libass ; les polices couvrent les accents français.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg fontconfig fonts-dejavu-core fonts-liberation \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p videos travail

CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
