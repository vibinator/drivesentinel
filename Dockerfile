FROM python:3.12-alpine

# smartmontools provides smartctl (with JSON output); tzdata for local timestamps in alerts
RUN apk add --no-cache smartmontools tzdata
# refresh the drive database so newer models are recognised (best effort)
RUN (update-smart-drivedb --no-verify 2>/dev/null || update-smart-drivedb 2>/dev/null) || true

WORKDIR /app
COPY app/ /app/

ENV DATA_DIR=/data \
    PORT=8090 \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8090

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD wget -qO- http://127.0.0.1:${PORT}/healthz || exit 1

CMD ["python", "main.py"]
