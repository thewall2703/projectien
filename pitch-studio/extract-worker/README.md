# Pitch Studio — PDF Extraction Worker (RunPod)

A RunPod serverless worker that downloads a PDF (presigned URL or DigitalOcean
Spaces key), extracts text page-by-page with **PyMuPDF**, falls back to
**Tesseract OCR** for image-only pages, de-duplicates repeated pages, and
optionally writes the JSON result back to Spaces.

Its `RUNPOD_ENDPOINT_ID` (returned after you deploy the endpoint below) is what
you set in the app's `.env`.

## Files

| File | Purpose |
| --- | --- |
| `extractor.py` | Pure extraction logic (text + OCR + hashing + chunking). Runnable as a CLI. |
| `handler.py` | RunPod serverless entrypoint (download → extract → optional upload). |
| `Dockerfile` | Image with `tesseract-ocr` + Python deps. |
| `requirements.txt` | Python deps. |
| `tests/test_extractor.py` | Unit tests (OCR/PDF path skipped if PyMuPDF missing). |

## Job input

```json
{
  "input": {
    "file_url": "https://<presigned-url>",     // OR "spaces_key": "reports/foo.pdf"
    "result_key": "extracted/foo.json",         // optional: upload result to Spaces
    "ocr": true,
    "ocr_lang": "eng",
    "min_chars": 40,
    "max_pages": null,
    "chunks": false
  }
}
```

Response: `{ page_count, ocr_pages, duplicate_pages, char_count, pages: [...], chunks?, result_key? }`.

## Storage env (for `spaces_key` / `result_key`)

```
SPACES_ENDPOINT=https://<region>.digitaloceanspaces.com
SPACES_REGION=<region>
SPACES_BUCKET=<bucket>
SPACES_KEY=<access-key>        # or AWS_ACCESS_KEY_ID
SPACES_SECRET=<secret-key>     # or AWS_SECRET_ACCESS_KEY
```

## Local test

```bash
cd pitch-studio/extract-worker
pip install -r requirements.txt          # needs system tesseract for OCR
pytest tests -q
python extractor.py /path/to/file.pdf --chunks   # quick smoke test
```

## Build & push the image

The worker must be built for **linux/amd64** (RunPod GPUs/CPUs are x86).

### Option A — DigitalOcean Container Registry

```bash
cd pitch-studio/extract-worker
doctl registry login
docker buildx build --platform linux/amd64 \
  -t registry.digitalocean.com/<your-registry>/pdf-extract-worker:latest \
  --push .
```

### Option B — Docker Hub

```bash
cd pitch-studio/extract-worker
docker login
docker buildx build --platform linux/amd64 \
  -t <dockerhub-user>/pdf-extract-worker:latest \
  --push .
```

## Deploy the RunPod endpoint

1. RunPod console → **Serverless** → **New Endpoint**.
2. **Container Image**: the tag you pushed above.
   - If using a private registry, add registry credentials in RunPod → Settings → Container Registry Auth.
3. **Worker type**: CPU is enough for text+OCR extraction (no GPU needed).
4. Set **Container Disk** to ~10–20 GB (large PDFs are streamed to `/tmp`).
5. Add the `SPACES_*` env vars if you use `spaces_key`/`result_key`.
6. Set **Max workers** (e.g. 3) and **Idle timeout** low to save cost.
7. Deploy → copy the **Endpoint ID**.

Then in the app `.env`:

```
RUNPOD_API_KEY=<your-runpod-api-key>
RUNPOD_ENDPOINT_ID=<endpoint-id-from-step-7>
```

## Invoke (sanity check)

```bash
curl -s -X POST "https://api.runpod.ai/v2/<ENDPOINT_ID>/runsync" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":{"file_url":"https://<presigned-pdf-url>","max_pages":3}}'
```
