FROM --platform=linux/amd64 python:3.11-slim

LABEL description="open-serve status page — public per-model health view modeled on status.huggingface.co"

WORKDIR /app

# Patch OS packages: the slim base periodically carries fixable HIGH CVEs
# (e.g. util-linux) that the release Trivy gate rightly rejects.
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y pip setuptools \
    && rm -rf /root/.cache

COPY main.py .
COPY templates ./templates
COPY static ./static

RUN useradd -r -u 1000 appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--log-level", "info"]
