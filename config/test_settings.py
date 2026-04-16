from .settings import *  # noqa: F401,F403


DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Faster tests
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# Keep production security settings from interfering with test HTTP client.
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_HSTS_SECONDS = 0

# Keep unit tests isolated from real multi-server PBIRS configuration.
POWERBI_REPORT_SERVER_URL = "http://test-pbirs.local"
POWERBI_REPORT_SERVER_URLS = [POWERBI_REPORT_SERVER_URL]
