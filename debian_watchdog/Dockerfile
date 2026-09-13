FROM python:3.13-alpine3.22
RUN apk add --no-cache android-tools \
    && pip install --no-cache-dir paho-mqtt==2.1.0
WORKDIR /app
COPY policy.py watchdog.py ./
ENV PYTHONUNBUFFERED=1 HOME=/data
USER 1000:1000
CMD ["python3", "/app/watchdog.py"]
