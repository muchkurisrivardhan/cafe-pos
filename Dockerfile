FROM python:3.13-slim
WORKDIR /app
COPY app.py index.html menu.csv ./
CMD ["python", "app.py", "--no-browser"]
