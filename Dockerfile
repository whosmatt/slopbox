# Playwright's own image: Chromium and its system libraries are already present
# and version-matched to the pip package, which is the fiddly part otherwise.
FROM mcr.microsoft.com/playwright/python:v1.62.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY LICENSE .

# The service writes only to /data, and only ever a fixed set of files.
RUN mkdir -p /data && chown -R pwuser:pwuser /data /app

USER pwuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
