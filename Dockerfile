# PF PrintFloor (Pomeroy Family PrintFloor) - status-only web dashboard for Bambu, Snapmaker U1/Klipper and Elegoo printers.
FROM python:3.13-slim

LABEL org.opencontainers.image.title="PF PrintFloor" \
      org.opencontainers.image.description="Status-only web dashboard for Bambu Lab, Snapmaker U1 / Klipper and Elegoo printers" \
      org.opencontainers.image.source="https://github.com/pomeroyfamily/pf-printfloor" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PRINTER_DASH_DATA=/data \
    PRINTER_DASH_HOST=0.0.0.0 \
    PRINTER_DASH_PORT=8765

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

# Runs as a non-root user (568). /data holds everything you set up; it must be writable by 568
# (a Docker named volume is, automatically; for a host folder: chown 568:568 <folder>).
RUN mkdir -p /data && chown 568:568 /data
USER 568:568
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/catalog' % os.environ.get('PRINTER_DASH_PORT','8765'), timeout=4)"

CMD ["python", "-m", "app"]
