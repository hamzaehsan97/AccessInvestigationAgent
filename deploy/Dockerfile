# Multi-stage; slim base; non-root user; DB mounted read-only at runtime.
FROM python:3.11-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/home/agent/.local/bin:$PATH

RUN adduser --disabled-password --gecos "" --uid 10001 agent
WORKDIR /app

# copy site-packages from builder
COPY --from=builder /root/.local /home/agent/.local
RUN chown -R agent:agent /home/agent/.local

COPY agent/ ./agent/
# Note: input_data/ is expected to be mounted at runtime as read-only.
# For a fully self-contained demo image you can `COPY input_data/ ./input_data/`.

USER agent
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://127.0.0.1:8080/healthz', timeout=3).raise_for_status()" || exit 1

# Default: FastAPI. Override with `docker run ... python -m agent investigate ...`
CMD ["python", "-m", "uvicorn", "agent.interfaces.server:app", "--host", "0.0.0.0", "--port", "8080"]
