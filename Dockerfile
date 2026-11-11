FROM python:3.12-slim
RUN pip install --no-cache-dir --root-user-action=ignore python-kasa==0.10.2
WORKDIR /app
COPY app.py plugs.py power.py index.html order.html rate.html settings.html ./
USER 65534
EXPOSE 8080
CMD ["python", "-u", "/app/app.py"]
