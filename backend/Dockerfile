# Backend image: FastAPI API + LangGraph support workflow + Chroma vector store.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first so this layer is cached across code changes.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the backend package (main.py, graph.py, prompts.py, data/kb seeds, ...).
COPY . /app

# Mount points for the named volumes. Creating them here keeps SQLite and Chroma
# writable even when the volumes are empty or not mounted (e.g. `docker run`).
RUN mkdir -p /app/chroma_db /app/data

EXPOSE 8000

# Locally this runs as `uvicorn main:app` from backend/; inside the container we
# bind 0.0.0.0 so the published port and the frontend container can reach it.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

# Probe a real API route; urllib raises on any non-2xx response.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/models', timeout=4)"
