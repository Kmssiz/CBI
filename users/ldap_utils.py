"""
LDAP Utilities for Active Directory Authentication and User Sync.

This module provides functions for authenticating users against Active Directory
and fetching user lists for synchronization.
"""

import logging
import ldap3
from ldap3 import Server, Connection, NONE, NTLM
from ldap3.core.exceptions import LDAPBindError, LDAPSocketOpenError, LDAPException
from django.conf import settings

logger = logging.getLogger('users')


def _get_ldap_server_candidates() -> list[tuple[str, int, bool]]:
    """
    Build ordered LDAP server/port candidates from settings.
    Tries primary first, then optional fallbacks.
    """
    host = settings.LDAP_SERVER_NAME
    port = getattr(settings, "LDAP_PORT", 389)
    use_ssl = getattr(settings, "LDAP_USE_SSL", False)
    alternates = getattr(settings, "LDAP_SERVER_ALTERNATES", [])
    enable_port_fallback = getattr(settings, "LDAP_ENABLE_PORT_FALLBACK", True)

    hosts = [host, *alternates]

    candidates: list[tuple[str, int, bool]] = []
    for item in hosts:
        candidates.append((item, port, use_ssl))
        if enable_port_fallback:
            # Common AD alternatives
            if (port, use_ssl) != (389, False):
                candidates.append((item, 389, False))
            if (port, use_ssl) != (636, True):
                candidates.append((item, 636, True))

    # Preserve order while removing duplicates
    deduped: list[tuple[str, int, bool]] = []
    seen = set()
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            deduped.append(candidate)
    return deduped


def _bind_connection(
    user_dn: str,
    password: str,
    authentication=None,
) -> Connection:
    """
    Attempt LDAP bind across configured candidates until one succeeds.
    """
    connect_timeout = getattr(settings, "LDAP_CONNECT_TIMEOUT", 8)
    receive_timeout = getattr(settings, "LDAP_RECEIVE_TIMEOUT", 20)
    last_error: LDAPException | None = None

    for host, port, use_ssl in _get_ldap_server_candidates():
        server = Server(
            host,
            port=port,
            use_ssl=use_ssl,
            connect_timeout=connect_timeout,
            get_info=NONE,
        )
        try:
            conn = Connection(
                server,
                user=user_dn,
                password=password,
                authentication=authentication,
                auto_bind=True,
                receive_timeout=receive_timeout,
            )
            logger.info(f"LDAP bind successful on {host}:{port} (ssl={use_ssl})")
            return conn
        except LDAPBindError:
            # Credentials issue: no point trying additional hosts/ports.
            raise
        except LDAPSocketOpenError as e:
            last_error = e
            logger.warning(f"LDAP socket error on {host}:{port} (ssl={use_ssl}): {e}")
            continue
        except LDAPException as e:
            last_error = e
            logger.warning(f"LDAP connection error on {host}:{port} (ssl={use_ssl}): {e}")
            continue

    if last_error:
        raise last_error
    raise LDAPException("No LDAP server candidates available.")


def connexion_ad2000(identifiant: str, password: str) -> dict | None:
    """
    Authenticates a user against Active Directory.

    Args:
        identifiant: User identifier (email or sAMAccountName).
        password: User's password.

    Returns:
        A dictionary with user info if successful, None otherwise.
    """
    ldap_domain = settings.LDAP_DOMAIN
    ldap_search_base = settings.LDAP_SEARCH_BASE

    # Determine if identifier is email or username
    if '@' in identifiant:
        user_dn = identifiant
        search_filter = f'(mail={identifiant})'
        auth_type = None
    else:
        user_dn = f"{ldap_domain}\\{identifiant}"
        search_filter = f'(sAMAccountName={identifiant})'
        auth_type = NTLM

    try:
        conn = _bind_connection(user_dn=user_dn, password=password, authentication=auth_type)
        if conn:
            conn.search(
                search_base=ldap_search_base,
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
    user_dn = f"{settings.LDAP_DOMAIN}\\{username}"
    ldap_search_base = settings.LDAP_SEARCH_BASE

    try:
        conn = _bind_connection(user_dn=user_dn, password=password, authentication=NTLM)

        # AD filter: person user accounts that are NOT disabled.
        # userAccountControl bit 2 (ACCOUNTDISABLE) must not be set.
        active_users_filter = (
            "(&(objectCategory=person)(objectClass=user)"
            "(!(userAccountControl:1.2.840.113556.1.4.803:=2)))"
        )

        # Use paged search to retrieve all users
        entry_generator = conn.extend.standard.paged_search(
            search_base=ldap_search_base,
            search_filter=active_users_filter,
            attributes=[
                'mail', 'sAMAccountName', 'company', 'department', 'name',
                'title', 'ipPhone', 'telephoneNumber', 'extensionAttribute1',
                'memberOf', 'userAccountControl'
            ],
            paged_size=500,  
            generator=True   
        )

        for entry in entry_generator:
            if entry['type'] != 'searchResEntry':
                continue
            
            attrs = entry['attributes']
            try:
                def is_account_disabled() -> bool:
                    raw_uac = attrs.get('userAccountControl')
                    if isinstance(raw_uac, list):
                        raw_uac = raw_uac[0] if raw_uac else None
                    try:
                        # ACCOUNTDISABLE flag is bit 2.
                        return bool(int(raw_uac) & 2)
                    except (TypeError, ValueError):
                        return False

                def get_attr(attr_name, is_list=False):
                    val = attrs.get(attr_name, "")
                    if is_list:
                        return val if isinstance(val, list) else [val] if val else []
                    if isinstance(val, list):
                        return str(val[0]) if val else ""
                    return str(val) if val else ""
                
                sam_account_name = get_attr('sAMAccountName')
                department = get_attr('department')

                # Defensive filtering: skip disabled users even if LDAP filter is bypassed.
                if is_account_disabled():
                    logger.debug(f"Skipping disabled AD account: {sam_account_name}")
                    continue

                if 'user_' not in sam_account_name or len(department) > 0:
                    member_of = get_attr('memberOf', is_list=True)
                    # Helper to extract CN from DN
                    ad_groups = []
                    for group_dn in member_of:
                        # group_dn looks like: CN=Marketing,OU=Groups,DC=example,DC=com
                        parts = group_dn.split(',')
                        if parts:
                            cn_part = parts[0] # CN=Marketing
                            if cn_part.upper().startswith("CN="):
                                ad_groups.append(cn_part[3:]) # Marketing
                            else:
                                ad_groups.append(cn_part)

                    user_list.append({
                        'mail': get_attr('mail'),
                        'sAMAccountName': sam_account_name,
                        'company': get_attr('company'),
                        'department': department,
                        'name': get_attr('name'),
                        'title': get_attr('title'),
                        'ad2000': get_attr('extensionAttribute1'),
                        'ad_groups': ad_groups,
                    })
            except (KeyError, IndexError, TypeError):
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
