FROM python:3.12-slim-bookworm
WORKDIR /app
COPY gateway/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY gateway/app.py ./app.py
RUN useradd --system --uid 10001 --no-create-home gateway
USER 10001
EXPOSE 4000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "4000", "--no-access-log"]

