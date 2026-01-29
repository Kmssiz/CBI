"""
LDAP Utilities for Active Directory Authentication and User Sync.

This module provides functions for authenticating users against Active Directory
and fetching user lists for synchronization.
"""

import logging
import ldap3
from ldap3 import Server, Connection, ALL, NTLM
from ldap3.core.exceptions import LDAPBindError, LDAPSocketOpenError, LDAPException
from django.conf import settings

logger = logging.getLogger('users')

LDAP_SERVER_NAME = settings.LDAP_SERVER_NAME
LDAP_DOMAIN = settings.LDAP_DOMAIN
LDAP_SEARCH_BASE = settings.LDAP_SEARCH_BASE


def connexion_ad2000(identifiant: str, password: str) -> dict | None:
    """
    Authenticates a user against Active Directory.

    Args:
        identifiant: User identifier (email or sAMAccountName).
        password: User's password.

    Returns:
        A dictionary with user info if successful, None otherwise.
    """
    server = Server(LDAP_SERVER_NAME, get_info=ALL)

    # Determine if identifier is email or username
    if '@' in identifiant:
        user_dn = identifiant
        search_filter = f'(mail={identifiant})'
    else:
        user_dn = f"{LDAP_DOMAIN}\\{identifiant}"
        search_filter = f'(sAMAccountName={identifiant})'

    try:
        conn = Connection(server, user=user_dn, password=password, auto_bind=True)
        if conn:
            conn.search(
                search_base=LDAP_SEARCH_BASE,
                search_filter=search_filter,
                search_scope=ldap3.SUBTREE,
                attributes=[
                    'mail', 'company', 'department', 'name', 'title',
                    'sAMAccountName', 'extensionAttribute1', 'employeeID', 'employeeNumber'
                ]
            )

            if conn.entries:
                entry = conn.entries[0]
                ad2000_value = str(entry.sAMAccountName) if hasattr(entry, 'sAMAccountName') else ""
                logger.debug(f"LDAP auth successful for user: {identifiant}, ad2000: {ad2000_value}")

                return {
                    'username': str(entry.sAMAccountName) if hasattr(entry, 'sAMAccountName') else identifiant,
                    'email': str(entry.mail) if hasattr(entry, 'mail') else "",
                    'first_name': str(entry.name).split(' ')[0] if hasattr(entry, 'name') else "",
                    'last_name': " ".join(str(entry.name).split(' ')[1:]) if hasattr(entry, 'name') else "",
                    'ad2000': ad2000_value,
                    'department': str(entry.department) if hasattr(entry, 'department') else "",
                    'title': str(entry.title) if hasattr(entry, 'title') else "",
                }
        return None

    except LDAPBindError as e:
        logger.warning(f"LDAP bind failed for {identifiant}: Invalid credentials")
        return None
    except LDAPSocketOpenError as e:
        logger.error(f"LDAP server connection failed: {e}")
        return None
    except LDAPException as e:
        logger.error(f"LDAP error during auth for {identifiant}: {e}")
        return None


def get_ad_users(username: str, password: str) -> list[dict]:
    """
    Fetches all users from Active Directory.

    Args:
        username: Admin username to bind with.
        password: Admin password.

    Returns:
        A list of user dictionaries, or empty list on failure.
    """
    user_list = []
    server = Server(LDAP_SERVER_NAME, get_info=ALL)
    user_dn = f"{LDAP_DOMAIN}\\{username}"

    try:
        conn = Connection(server, user=user_dn, password=password, authentication=NTLM, auto_bind=True)

        conn.search(
            search_base=LDAP_SEARCH_BASE,
            search_filter='(objectclass=person)',
            attributes=[
                'mail', 'sAMAccountName', 'company', 'department', 'name',
                'title', 'ipPhone', 'telephoneNumber', 'extensionAttribute1'
            ]
        )

        for entry in conn.entries:
            try:
                sam_account_name = str(entry.sAMAccountName) if hasattr(entry, 'sAMAccountName') else ""
                department = str(entry.department) if hasattr(entry, 'department') else ""

                # Filter out service accounts
                if 'user_' not in sam_account_name or len(department) > 0:
                    user_list.append({
                        'mail': str(entry.mail) if hasattr(entry, 'mail') else "",
                        'sAMAccountName': sam_account_name,
                        'company': str(entry.company) if hasattr(entry, 'company') else "",
                        'department': department,
                        'name': str(entry.name) if hasattr(entry, 'name') else "",
                        'title': str(entry.title) if hasattr(entry, 'title') else "",
                        'ad2000': str(entry.extensionAttribute1) if hasattr(entry, 'extensionAttribute1') else "",
                    })
            except AttributeError:
                # Skip entries with missing required attributes
                continue

        logger.info(f"LDAP sync completed. Retrieved {len(user_list)} users.")
        return user_list

    except LDAPBindError:
        logger.error(f"LDAP sync failed: Invalid credentials for {username}")
        return []
    except LDAPSocketOpenError as e:
        logger.error(f"LDAP sync failed: Server connection error - {e}")
        return []
    except LDAPException as e:
        logger.error(f"LDAP sync error: {e}")
        return []
