# 由 repo 根層建置（config/ 在 excel-service/ 之外）：
#   docker build -f excel-service/Dockerfile -t bamhi-excel .
FROM python:3.12-slim

WORKDIR /app
COPY excel-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY excel-service/ .
COPY config/ ./config/

ENV PORT=8080
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}
