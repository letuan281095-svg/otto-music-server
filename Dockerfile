FROM python:3.11-slim

# Cài đặt ffmpeg + nodejs (JS runtime cho yt-dlp giải mã YouTube)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg nodejs ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Tạo user không phải root (yêu cầu của Hugging Face Spaces)
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH

WORKDIR $HOME/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY --chown=user app.py .

EXPOSE 5005

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-5005}"]
