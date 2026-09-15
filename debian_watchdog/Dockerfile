FROM python:3.13-alpine3.22
RUN apk add --no-cache android-tools openssh-client-default \
    && pip install --no-cache-dir paho-mqtt==2.1.0 python-kasa==0.10.2 \
    && addgroup -g 1000 app \
    && adduser -D -u 1000 -G app app
WORKDIR /app
COPY policy.py tapo_power.py watchdog.py ./
ENV PYTHONUNBUFFERED=1 HOME=/data
USER 1000:1000
CMD ["python3", "/app/watchdog.py"]
