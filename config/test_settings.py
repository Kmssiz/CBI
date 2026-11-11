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

# Tests must never reach the real directory, report servers, Odoo or mail
# server, even when the local .env points at production.
LDAP_SERVER_NAME = "ldap.invalid"
LDAP_SERVER_ALTERNATES = []
LDAP_SERVICE_USERNAME = ""
LDAP_SERVICE_PASSWORD = ""
LDAP_GROUP_MEMBERS_URL = "http://ldap-api.invalid"
LDAP_API_TOKEN = "test-token"
ODOO_DB_HOST = ""
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
