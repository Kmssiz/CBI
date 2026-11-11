"""
Encrypted storage of the user's directory password in the Django session.

PBIRS needs the user's own credentials (NTLM), so the password has to be kept
for the length of the session. Database sessions are only signed, not
encrypted, so the value is encrypted with a key derived from SECRET_KEY: a
leaked ``django_session`` table or database backup no longer exposes passwords.
"""
import base64
import hashlib
import logging
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

logger = logging.getLogger('users')

SESSION_KEY = 'ldap_password_enc'
LEGACY_SESSION_KEY = 'ldap_password'  # plaintext, written by earlier versions


def _fernet() -> Fernet:
    digest = hashlib.sha256(f'cbi-session-password:{settings.SECRET_KEY}'.encode('utf-8')).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def store_ldap_password(request, password: str) -> None:
    request.session[SESSION_KEY] = _fernet().encrypt(password.encode('utf-8')).decode('ascii')
    request.session.pop(LEGACY_SESSION_KEY, None)


def get_ldap_password(request) -> Optional[str]:
    """The user's password, or None when the session has none (or it cannot be read)."""
    session = request.session
    legacy = session.get(LEGACY_SESSION_KEY)
    if legacy:  # session opened before the upgrade: encrypt it in place
        store_ldap_password(request, legacy)
        return legacy
    token = session.get(SESSION_KEY)
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode('ascii')).decode('utf-8')
    except (InvalidToken, ValueError):
        logger.warning("Stored session password could not be decrypted (SECRET_KEY changed?).")
        return None
