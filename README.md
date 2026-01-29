# CBI - Power BI Report Server Integration

A Django application for integrating Power BI Report Server (PBIRS) with custom folder management and user permissions.

## Prerequisites

- Python 3.11+
- Access to a Power BI Report Server instance
- LDAP/Active Directory for authentication

## Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/Kmssiz/CBI.git
cd CBI
```

### 2. Create Virtual Environment

```bash
python -m venv venv

# Windows
venv\Scripts\activate

# Linux/macOS
source venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure Environment Variables

Create a `.env` file in the project root:

```env
# Django Settings
SECRET_KEY=your-secret-key-here
DEBUG=True
ALLOWED_HOSTS=127.0.0.1,localhost

# Power BI Report Server
POWERBI_REPORT_SERVER_URL=http://your-pbirs-server

# LDAP Configuration
LDAP_SERVER_NAME=your-ldap-server
LDAP_DOMAIN=YOUR-DOMAIN
LDAP_SEARCH_BASE=DC=example,DC=com
```

### 5. Run Migrations

```bash
python manage.py migrate
```

### 6. Create Superuser (Optional)

```bash
python manage.py createsuperuser
```

### 7. Start Development Server

```bash
python manage.py runserver
```

The application will be available at `http://127.0.0.1:8000`

---

## Production Deployment

For production, use a production-grade WSGI server instead of the Django development server.

### Using Docker (Linux)

```bash
# Build the image
docker build -t cbi-app .

# Run the container
docker run -p 8000:8000 --env-file .env cbi-app
```

### Linux/macOS (Gunicorn)

```bash
# Run migrations first
python manage.py migrate

# Start gunicorn
gunicorn --bind 0.0.0.0:8000 --workers 4 config.wsgi:application
```

### Windows (Waitress)

> **Note**: Gunicorn does not work on Windows. Use **waitress** instead.

```bash
# Run migrations first
python manage.py migrate

# Start waitress
waitress-serve --port=8000 config.wsgi:application
```

---

## Project Structure

```
CBI/
├── config/              # Django project settings
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── powerbi_report/      # Main app for PBIRS integration
│   ├── services/        # Service layer for API calls
│   │   └── pbirs_client.py
│   ├── models.py
│   └── views.py
├── users/               # User authentication and management
│   ├── ldap_utils.py    # LDAP authentication
│   └── views.py
├── notifications/       # In-app notifications
├── templates/           # HTML templates
├── static/              # Static files (CSS, JS)
├── requirements.txt     # Python dependencies
└── Dockerfile           # Docker configuration
```

---

## Environment Variables Reference

| Variable | Description | Required |
|----------|-------------|----------|
| `SECRET_KEY` | Django secret key | Yes |
| `DEBUG` | Debug mode (True/False) | No (default: False) |
| `ALLOWED_HOSTS` | Comma-separated list of hosts | No (default: 127.0.0.1,localhost) |
| `POWERBI_REPORT_SERVER_URL` | PBIRS base URL | Yes |
| `LDAP_SERVER_NAME` | LDAP server hostname | Yes |
| `LDAP_DOMAIN` | AD domain name | Yes |
| `LDAP_SEARCH_BASE` | LDAP search base DN | Yes |

---

## Troubleshooting

### LDAP Authentication Issues

- Ensure `LDAP_SERVER_NAME` is reachable from your server
- Check firewall rules for LDAP port (389 or 636 for LDAPS)
- Verify `LDAP_DOMAIN` matches your AD domain

### PBIRS Connection Issues

- Verify `POWERBI_REPORT_SERVER_URL` is correct
- Ensure the server allows NTLM authentication
- Check if your user has permissions on the PBIRS instance
