FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml README.md LICENSE ./
COPY dvlib ./dvlib

RUN python -m pip install --no-cache-dir --upgrade "pip>=26.1.2" "setuptools>=83" \
    && python -m pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 dvlib \
    && mkdir -p /data \
    && chown dvlib:dvlib /data

ENV DVLIB_DATA_DIR=/data
VOLUME /data

EXPOSE 8000

USER dvlib

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3)"

CMD ["dvlib-server", "run", "--data-dir", "/data", "--host", "0.0.0.0", "--port", "8000"]
