# POCT Hub MVP for Cloud Run. RESEARCH AND DECISION-SUPPORT ONLY. Synthetic data only.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PORT=8080 \
    DATABASE_URL=sqlite:////tmp/poct_hub.db SEED_ON_START=true
WORKDIR /app
COPY requirements.txt .
RUN pip install --root-user-action=ignore -r requirements.txt
COPY poct_hub ./poct_hub
COPY seed_synthetic.py .
RUN useradd -r -u 10001 poct
USER poct
EXPOSE 8080
# Cloud Run injects $PORT; one worker so the single SQLite file has one writer process.
CMD exec uvicorn poct_hub.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips '*'
