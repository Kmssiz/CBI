"""
Active Directory -> local accounts.

Shared by the "Synchroniser les utilisateurs" button and the hourly worker, so that new people appear
without an administrator having to click, and so that accounts disabled in AD stop having access.
"""
import logging
from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone

from .models import CustomUser, MobileApiSession, Role

logger = logging.getLogger("users")


@dataclass
class DirectorySyncResult:
    created: int = 0
    existing: int = 0
    skipped: int = 0


def normalize_ad_groups(raw_groups) -> list[str]:
    """Normalize an LDAP group payload into a clean list of strings."""
    if not raw_groups:
        return []
    if not isinstance(raw_groups, list):
        raw_groups = [raw_groups]
    return [str(group).strip() for group in raw_groups if str(group).strip()]


def sync_directory_users(ldap_users: list[dict]) -> DirectorySyncResult:
    """Create the accounts that are missing and fill the blanks of the existing ones (bulk lookups)."""
    from .utils import clean_directory_value

    result = DirectorySyncResult()
    all_db_users = CustomUser.objects.all().only('id', 'username', 'ad2000', 'pole', 'direction', 'societe', 'ad_groups', 'ldap_created_at')
    username_map = {u.username.lower(): u for u in all_db_users}
    ad2000_map = {u.ad2000.lower(): u for u in all_db_users if u.ad2000}

    user_role, _ = Role.objects.get_or_create(name=settings.USER_ROLE_NAME)

    for ldap_user in ldap_users:
        sam_account = clean_directory_value(ldap_user.get("sAMAccountName"))
        # extensionAttribute1 first; the account name when it is empty (or LDAP's "[]")
        ad2000 = clean_directory_value(ldap_user.get("ad2000")) or sam_account
        ldap_groups = normalize_ad_groups(ldap_user.get("ad_groups", []))
        company = clean_directory_value(ldap_user.get("company"))
        department = clean_directory_value(ldap_user.get("department"))
        full_name = clean_directory_value(ldap_user.get("name")).split(' ')

        if not ad2000:
            result.skipped += 1
            continue

        user = ad2000_map.get(ad2000.lower()) or username_map.get(sam_account.lower())

        if not user:
            try:
                user = CustomUser(
                    username=sam_account or ad2000,
                    ad2000=ad2000,
                    societe=company,
                    pole=company,
                    direction=department,
                    first_name=full_name[0],
                    last_name=" ".join(full_name[1:]),
                    email=clean_directory_value(ldap_user.get("mail")),
                    ad_groups=ldap_groups,
                    ldap_created_at=ldap_user.get("created"),
                    role=user_role,
                    status="Not Active",
                    can_view_direction=False,
                )
                user.save()
                user.user_permissions.set(user_role.permissions.all())
                result.created += 1
                username_map[user.username.lower()] = user
                if user.ad2000:
                    ad2000_map[user.ad2000.lower()] = user
            except Exception:  # one bad row must not stop the others
                logger.exception("Échec de l'enregistrement de l'utilisateur LDAP synchronisé %s", sam_account)
            continue

        update_fields = {}
        if not user.pole and company:
            update_fields["pole"] = company
        if not user.direction and department:
            update_fields["direction"] = department
        if not user.societe and company:
            update_fields["societe"] = company
        if ldap_user.get("created") and user.ldap_created_at != ldap_user["created"]:
            update_fields["ldap_created_at"] = ldap_user["created"]
        if ldap_groups and set(normalize_ad_groups(user.ad_groups)) != set(ldap_groups):
            update_fields["ad_groups"] = ldap_groups
        if update_fields:
            CustomUser.objects.filter(pk=user.pk).update(**update_fields)
        result.existing += 1

    logger.info(
        "Résumé de la synchronisation LDAP total=%s créés=%s existants=%s ignorés=%s",
        len(ldap_users), result.created, result.existing, result.skipped,
    )
    return result


def deactivate_missing_users(ldap_users: list[dict], max_share: float = 0.1) -> int:
    """
    Switch off the accounts that AD no longer lists as active (disabled or removed people), and end their
    mobile sessions. Only directory accounts are concerned: administrators and accounts without an AD2000
    are never touched. A directory answer that would switch off more than ``max_share`` of the accounts
    is treated as a broken answer, not as a wave of departures, and changes nothing.
    """
    from .utils import clean_directory_value

    present = set()
    for entry in ldap_users:
        for key in ("sAMAccountName", "ad2000"):
            value = clean_directory_value(entry.get(key)).lower()
            if value:
                present.add(value)
    if not present:
        return 0

    candidates = [
        user for user in CustomUser.objects.filter(is_active=True, is_superuser=False)
        .exclude(ad2000__isnull=True).exclude(ad2000='').select_related('role')
        if not user.is_admin and 'user_' not in user.username.lower()
    ]
    missing = [
        user for user in candidates
        if user.username.lower() not in present and (user.ad2000 or '').lower() not in present
    ]
    if not missing:
        return 0
    if len(missing) > max(5, int(len(candidates) * max_share)):
        logger.error(
            "Directory sync would deactivate %s of %s accounts: treated as a faulty answer, nothing changed.",
            len(missing), len(candidates),
        )
        return 0

    ids = [user.pk for user in missing]
    CustomUser.objects.filter(pk__in=ids).update(is_active=False, status="Not Active")
    MobileApiSession.objects.filter(user_id__in=ids, revoked_at__isnull=True).update(revoked_at=timezone.now())
    logger.warning("Deactivated %s account(s) that are no longer active in the directory.", len(ids))
    return len(ids)
