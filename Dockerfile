FROM ghcr.io/astral-sh/uv:0.13.5-python3.13-trixie-slim
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
ENV UV_PYTHON_DOWNLOADS=never
RUN uv sync --frozen --no-dev --no-editable --no-cache \
    && groupadd --gid 10001 tracker \
    && useradd --uid 10001 --gid tracker --no-log-init tracker \
    && mkdir -p /data /config \
    && chown tracker:tracker /data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER 10001:10001
VOLUME ["/data"]
ENTRYPOINT ["reset-tracker"]
CMD ["--config", "/config/config.toml", "run"]
