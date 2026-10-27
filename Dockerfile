FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir '.[postgres]' && groupadd --gid 10002 ai-memory && useradd --uid 10002 --gid 10002 --shell /usr/sbin/nologin --create-home ai-memory
RUN python -m compileall -q /app/src
USER 10002:10002
CMD ["uvicorn", "ai_memory.api:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
