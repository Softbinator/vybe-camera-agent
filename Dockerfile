FROM python:3.12-slim

# Install ffmpeg only (rclone is no longer used; uploads go via the backend API)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY src/ ./src/

CMD ["python", "main.py"]
