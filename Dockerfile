FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
# Optional enterprise CA bundle; TLS verification remains enabled.
RUN --mount=type=secret,id=pip_ca \
    if [ -f /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi; \
    pip install --no-cache-dir -r requirements.txt
RUN useradd --create-home app && mkdir /data && chown app:app /data
COPY --chown=app:app src ./src
COPY --chown=app:app templates ./templates
COPY --chown=app:app scripts ./scripts
USER app
ENV SQLITE_DB_PATH=/data/oil_bot.db PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
