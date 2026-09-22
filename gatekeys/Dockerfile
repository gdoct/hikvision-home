FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    # DejaVu is what the printed card is set in; the slim image has no fonts.
    && apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY app/ ./app/

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8097
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8097", "--proxy-headers", "--forwarded-allow-ips", "*"]
