import ldap3
from ldap3 import Server, Connection, ALL, NTLM
from ldap3.core.exceptions import LDAPCursorError
from django.conf import settings
import json
import re

# Hardcoded settings based on user snippet suitable for the Hasnaoui environment
LDAP_SERVER_NAME = 'ldap.groupe-hasnaoui.com'
LDAP_DOMAIN = 'GROUPE-HASNAOUI'
LDAP_SEARCH_BASE = "dc=groupe-hasnaoui,dc=local"

def connexion_ad2000(identifiant, password):
    """
    Authenticates a user against Active Directory.
    Returns a dictionary with user info if successful, None otherwise.
    """
    server = Server(LDAP_SERVER_NAME, get_info=ALL)
    
    # Try to determine if identifiant is email or samaccountname
    if '@' in identifiant:
        # For email, bind with the email directly
        user_dn = identifiant
        search_filter = f'(mail={identifiant})'
    else:
        # For username, prepend domain
        user_dn = f"{LDAP_DOMAIN}\\{identifiant}"
        search_filter = f'(sAMAccountName={identifiant})'
    
    try:
        conn = Connection(server, user=user_dn, password=password, auto_bind=True)
        if conn:
            conn.search(search_base=LDAP_SEARCH_BASE,
                        search_filter=search_filter,
                        search_scope=ldap3.SUBTREE,
                        attributes=['mail', 'company', 'department', 'name', 'title', 'sAMAccountName', 'extensionAttribute1'])
            
            if conn.entries:
                entry = conn.entries[0]
                return {
                    'username': str(entry.sAMAccountName) if hasattr(entry, 'sAMAccountName') else identifiant,
                    'email': str(entry.mail) if hasattr(entry, 'mail') else "",
                    'first_name': str(entry.name).split(' ')[0] if hasattr(entry, 'name') else "", # Rough approx
                    'last_name': " ".join(str(entry.name).split(' ')[1:]) if hasattr(entry, 'name') else "",
                    'ad2000': str(entry.extensionAttribute1) if hasattr(entry, 'extensionAttribute1') else "", # Adjust if needed
                    'department': str(entry.department) if hasattr(entry, 'department') else "",
                    'title': str(entry.title) if hasattr(entry, 'title') else "",
                }
        return None
    except Exception as e:
        print(f"LDAP Auth Error: {e}")
        return None

def get_ad_users(username, password):
    """
    Fetches all users from Active Directory.
    Requires a valid user/pass to bind.
    """
    liste = []
    server = Server(LDAP_SERVER_NAME, get_info=ALL)
    user_dn = f"{LDAP_DOMAIN}\\{username}"
    
    try:
        conn = Connection(server, user=user_dn, password=password, authentication=NTLM, auto_bind=True)
        
        conn.search(search_base=LDAP_SEARCH_BASE,
                    search_filter='(objectclass=person)',
                    attributes=['mail', 'sAMAccountName', 'company', 'department', 'name', 'title', 'ipPhone', 'telephoneNumber', 'extensionAttribute1'])

        for e in conn.entries:
            try:
                # Filter logic from snippet
                sAMAccountName = str(e.sAMAccountName) if hasattr(e, 'sAMAccountName') else ""
                department = str(e.department) if hasattr(e, 'department') else ""
                
                if 'user_' not in sAMAccountName or len(department) > 0:
                    liste.append({
                        'mail': str(e.mail) if hasattr(e, 'mail') else "",
                        'sAMAccountName': sAMAccountName,
                        'company': str(e.company) if hasattr(e, 'company') else "",
                        'department': department,
                        'name': str(e.name) if hasattr(e, 'name') else "",
                        'title': str(e.title) if hasattr(e, 'title') else "",
                        'ad2000': str(e.extensionAttribute1) if hasattr(e, 'extensionAttribute1') else "", # frequent mapping
                    })
            except Exception:
                pass
                
        return liste
    except Exception as e:
        print(f"LDAP Sync Error: {e}")
        return []
