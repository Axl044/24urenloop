FROM python:3.13-alpine
RUN apk add --no-cache tzdata
WORKDIR /app
COPY server.py ./
COPY static ./static
ENV DB_PATH=/data/24urenloop.db PORT=8080 PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1:8080/healthz || exit 1
CMD ["python", "server.py"]
