FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# yt-dlp нужен JS-рантайм для расшифровки подписей YouTube
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Анализ звучания (эмбеддер). По умолчанию PyTorch для CPU;
# для видеокарты NVIDIA соберите с --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu128
# и раскомментируйте блок deploy в docker-compose.yml. EMBEDDINGS=0 — без эмбеддера.
ARG EMBEDDINGS=1
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
RUN if [ "$EMBEDDINGS" = "1" ]; then \
      pip install --no-cache-dir torch --index-url "$TORCH_INDEX" && \
      pip install --no-cache-dir transformers ; \
    fi

COPY app ./app
COPY web ./web

ENV MUSIC_DIR=/music DATA_DIR=/data WEB_DIR=/srv/web PYTHONUNBUFFERED=1 HF_HOME=/data/hf
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
