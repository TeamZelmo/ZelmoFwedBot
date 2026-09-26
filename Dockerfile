FROM python:3.11-slim

WORKDIR /app

# system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libssl-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY forwarder_bot.py .

# session dir (persist via volume)
RUN mkdir -p /app/sessions
VOLUME ["/app/sessions"]

ENV PYTHONUNBUFFERED=1

CMD ["python", "forwarder_bot.py"]
