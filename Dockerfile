FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

# Install only runtime libraries. All Python dependencies currently resolve to
# prebuilt wheels, so a compiler toolchain is not needed in the application image.
RUN apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    apt-transport-https \
    unixodbc \
    libpq5 \
    libjpeg62-turbo \
    zlib1g \
    && rm -rf /var/lib/apt/lists/*

# Install Microsoft ODBC SQL Server drivers (Linux runtime for pyodbc).
# Microsoft publishes the Debian 12 repository, which is compatible with the
# Debian-based Python image used here.
RUN set -eux; \
    apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update && apt-get install -y --no-install-recommends curl ca-certificates gnupg2; \
    curl --fail --show-error --silent --location --retry 3 --connect-timeout 10 --max-time 60 https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg; \
    echo "deb [arch=amd64,arm64,armhf signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql-release.list; \
    apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update; \
    ACCEPT_EULA=Y apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 install -y --no-install-recommends msodbcsql17 msodbcsql18 unixodbc-dev; \
    rm -rf /var/lib/apt/lists/*

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Set work directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

# Copy project files
COPY . .

# Do not run the web application as root.
RUN addgroup --system cbi && adduser --system --ingroup cbi cbi \
    && chown -R cbi:cbi /app
USER cbi

# Expose Django port
EXPOSE 8000

# Run Django app with gunicorn (production-ready)
# Note: Run migrations separately before starting (e.g., in CI/CD or entrypoint script)
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", "--timeout", "300", "--graceful-timeout", "60", "config.wsgi:application"]
