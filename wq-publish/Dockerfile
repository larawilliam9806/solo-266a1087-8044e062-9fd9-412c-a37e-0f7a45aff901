FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY wsgi.py ./
COPY wq_publish ./wq_publish

# 数据（SQLite 登记库）与下游供数目录，容器内默认位置，可用环境变量覆盖
ENV WQ_DATA_DIR=/app/data \
    WQ_DOWNSTREAM_DIR=/app/downstream

EXPOSE 5000

# 单进程多线程即可：并发正确性依赖 SQLite BEGIN IMMEDIATE 与同目录原子改名
CMD ["waitress-serve", "--host=0.0.0.0", "--port=5000", "--call", "wsgi:create_app"]
