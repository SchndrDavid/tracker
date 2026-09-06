FROM python:3.12-slim

WORKDIR /app

RUN mkdir -p /data /app && chown -R 1000:1000 /data /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY static ./static

USER 1000:1000

ENV DAYLOG_DB=/data/daylog.db
EXPOSE 8000

CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
