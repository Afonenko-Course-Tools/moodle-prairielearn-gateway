FROM python@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
COPY requirements.lock /app/requirements.lock
RUN python -m pip install --no-cache-dir --no-deps -r /app/requirements.lock
COPY src /app/src
COPY schema /app/schema
COPY tools/run-gateway.py /app/tools/run-gateway.py
USER 1000:1000
EXPOSE 18080
ENTRYPOINT ["python", "/app/tools/run-gateway.py", "--config", "/run/config/runtime.json"]
