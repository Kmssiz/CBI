# AGENTS.md - AI Agent Guidelines

This document provides context and guidelines for AI agents (like GitHub Copilot, Claude, Cursor, etc.) working on this codebase.

## Project Overview

CBI is a Django application that integrates Power BI Report Server (PBIRS) with a custom frontend. It provides:
- LDAP/Active Directory authentication
- PBIRS report embedding and management
- Custom virtual folder organization
- User permission management

## Technology Stack

- **Backend**: Django 5.1+
- **Authentication**: LDAP with NTLM for PBIRS
- **Database**: SQLite (development), PostgreSQL (production recommended)
- **Production Server**: Gunicorn
- **APIs**: PBIRS REST API v2.0

## Key Architectural Decisions

### Service Layer Pattern
All PBIRS API interactions go through `powerbi_report/services/pbirs_client.py`. This centralizes:
- Authentication setup
- Error handling
- Caching
- Logging

When adding new PBIRS functionality, add methods to `PBIRSClient` rather than making direct API calls in views.

### Session-Based Password Storage
User passwords are stored in the Django session (not the database) for NTLM authentication to PBIRS. This is **intentional** because:
1. PBIRS requires user credentials for NTLM auth
2. We cannot use service accounts due to permission delegation requirements
3. Session storage is encrypted and temporary

Do NOT move password storage to the database.

### Role Constants
Hardcoded role strings are centralized in `settings.py`:
- `settings.ADMIN_ROLE_NAME` = "admin"
- `settings.USER_ROLE_NAME` = "user"

Use these constants instead of inline strings.

## Code Style Guidelines

### Logging
Use the `logging` module, not `print()`:
```python
import logging
logger = logging.getLogger('powerbi_report')  # or 'users'

logger.info("User logged in")
logger.error(f"Failed to fetch report: {e}")
```

### Error Handling
Catch specific exceptions, not bare `except`:
```python
# Bad
try:
    do_something()
except Exception as e:
    print(e)

# Good
from ldap3.core.exceptions import LDAPBindError
try:
    do_something()
except LDAPBindError:
    logger.warning("Invalid credentials")
except LDAPSocketOpenError as e:
    logger.error(f"Server unreachable: {e}")
```

### Type Hints
Use type hints for function signatures:
```python
def get_report(report_id: str) -> Optional[dict]:
    ...
```

## File Organization

```
powerbi_report/
├── services/           # Service layer (API clients)
│   ├── __init__.py
│   └── pbirs_client.py
├── views.py            # View functions
├── models.py           # Django models
└── urls.py             # URL routing

users/
├── ldap_utils.py       # LDAP authentication
├── views.py            # User management views
└── models.py           # CustomUser model
```

## Common Tasks

### Adding a New PBIRS API Method
1. Add the method to `PBIRSClient` in `powerbi_report/services/pbirs_client.py`
2. Use `_make_request()` for the HTTP call
3. Add proper logging and error handling
4. Use the method in views via `client = PBIRSClient(request)`

### Adding a New View
1. Add the function to `powerbi_report/views.py`
2. Use `@login_required` decorator
3. Get PBIRS data via `PBIRSClient`
4. Include notifications context:
   ```python
   notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
   unread = notifications.filter(is_read=False).count()
   permissions = get_user_permissions(request.user)
   ```

## Testing

Run Django checks:
```bash
python manage.py check
python manage.py migrate --check
```

## Development Commands

```bash
# Start dev server
python manage.py runserver

# Create migrations
python manage.py makemigrations

# Apply migrations
python manage.py migrate

# Freeze dependencies
pip freeze > requirements.txt
```
