FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY xmgate xmgate
COPY webapp webapp
RUN useradd --system --uid 1000 xmgate && mkdir -p /data && chown xmgate /data
USER xmgate
ENV DB_PATH=/data/xmgate.sqlite3 PORT=8080
EXPOSE 8080
CMD ["python", "-m", "xmgate"]
