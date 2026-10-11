# syntax=docker/dockerfile:1
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt ./
# Optional trusted CA bundle for build environments with a TLS inspection proxy.
# It is mounted only during installation, never baked into the image.
RUN --mount=type=secret,id=build_ca,required=false \
    if [ -s /run/secrets/build_ca ]; then \
      PIP_CERT=/run/secrets/build_ca pip install --no-cache-dir -r requirements.txt; \
    else pip install --no-cache-dir -r requirements.txt; fi
COPY calibre ./calibre
COPY web ./web
COPY config.py main.py web_app.py ./
RUN chmod -R a+rX /app
EXPOSE 8090
CMD ["python", "web_app.py", "--host", "0.0.0.0", "--port", "8090", "--config", "/data/config.json"]
