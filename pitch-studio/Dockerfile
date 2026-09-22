FROM node:20-alpine AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps chromium
COPY backend ./backend
COPY --from=frontend /frontend/dist ./frontend/dist
EXPOSE 8080
CMD ["sh", "-c", "python -m backend.boot && uvicorn backend.main:app --host 0.0.0.0 --port 8080"]
