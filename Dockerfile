FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    NFS_DIR=/mnt/nfs

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 fileloader \
    && useradd --uid 10001 --gid fileloader --home-dir /nonexistent --shell /usr/sbin/nologin fileloader \
    && mkdir -p /data /mnt/nfs \
    && chown -R fileloader:fileloader /app /data /mnt/nfs

COPY --chown=fileloader:fileloader app.py ./
COPY --chown=fileloader:fileloader templates ./templates
COPY --chown=fileloader:fileloader static ./static

USER fileloader:fileloader
EXPOSE 8080
CMD ["gunicorn", "--no-control-socket", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "8", "--timeout", "0", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
