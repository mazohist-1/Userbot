FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/app/data
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY userbot ./userbot
RUN mkdir -p /app/data
VOLUME /app/data

# heartbeat обновляется после каждой успешной проверки связи (раз в 5 минут)
HEALTHCHECK --interval=5m --timeout=10s --start-period=2m \
  CMD python -c "import time,sys; sys.exit(time.time()-int(open('/app/data/heartbeat').read())>900)"

CMD ["python", "-m", "userbot"]
