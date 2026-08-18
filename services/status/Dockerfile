FROM --platform=linux/amd64 python:3.11-slim

LABEL description="open-serve status page — public per-model health view modeled on status.huggingface.co"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && rm -rf /root/.cache

COPY main.py .
COPY templates ./templates
COPY static ./static

RUN useradd -r -u 1000 appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--log-level", "info"]
