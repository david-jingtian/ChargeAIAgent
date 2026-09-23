FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY src ./src
RUN pip install --no-cache-dir --no-deps .
COPY schema.sql ./schema.sql
COPY scripts ./scripts
COPY tests ./tests
USER 10001
CMD ["uvicorn", "charge_agent.api:app", "--host", "0.0.0.0", "--port", "8000"]
