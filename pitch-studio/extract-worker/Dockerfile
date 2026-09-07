# RunPod serverless worker: PDF text extraction + OCR fallback
FROM python:3.12-slim

# Tesseract for OCR of image-only PDF pages.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        libgl1 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY extractor.py handler.py ./

# RunPod invokes the handler via the serverless runtime started in handler.py
CMD ["python", "-u", "handler.py"]
