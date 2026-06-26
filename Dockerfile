FROM python:3.13-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project
COPY . .

# Collect static files at build time so whitenoise can serve them
RUN python manage.py collectstatic --noinput

EXPOSE 8000

# FRONTEND_LEAGUE_ID defaults to 1; override at runtime with -e or docker-compose env
ENV FRONTEND_LEAGUE_ID=1

CMD ["gunicorn", "leagues.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2"]
