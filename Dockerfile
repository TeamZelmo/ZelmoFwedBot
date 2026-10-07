FROM python:3.11-slim

WORKDIR /app

# system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libssl-dev \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY forwarder_bot.py .

# session directory persistence
RUN mkdir -p /app/sessions
VOLUME ["/app/sessions"]

ENV PYTHONUNBUFFERED=1

CMD ["python", "forwarder_bot.py"]
