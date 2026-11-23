FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# дефолтный промпт в образе: приложение не должно падать без bind-mount
COPY prompt.txt ./prompt.txt
RUN chown -R app:app /app

# не root: приложению нужен только /data (bind-mount) и /tmp
USER app
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "app.main"]
