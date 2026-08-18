FROM --platform=linux/amd64 python:3.11-slim

LABEL description="open-serve gateway — API key authentication, backend routing, usage metrics"

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && rm -rf /root/.cache

# Copy application
COPY main.py .

# Non-root user
RUN useradd -r -u 1000 appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--log-level", "info"]
