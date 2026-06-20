FROM python:3.12-slim AS base
WORKDIR /app
RUN addgroup --system app && adduser --system --group app

FROM base AS deps
COPY pyproject.toml .
RUN pip install --no-cache-dir .

FROM deps AS final
COPY --chown=app:app . .
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
