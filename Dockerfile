# Slim multi-stage image for the HFA 24-2 TD editor.
# Stdlib-only; no Python deps to install. Final image ~50 MB.
FROM python:3.12-slim

# Stop Python from buffering stdout (so platform log streaming works) and
# from writing .pyc files into the container.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8766

WORKDIR /app

COPY app/ /app/

# Persistent data goes on a volume so subjects + edits survive redeploys.
RUN mkdir -p /data/images /data/extracted
VOLUME ["/data"]

EXPOSE 8766
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request,sys,os; \
                 urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8766')+'/health').read()" \
       || exit 1

CMD ["python3", "server.py"]
