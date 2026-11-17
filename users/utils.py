from functools import wraps

from django.core.exceptions import PermissionDenied
from django.utils.timezone import now

from .models import UserHistory

def admin_required(view_func):
    """
    Decorator for views that checks if the user is an admin.
    Raises PermissionDenied (403) if the user is not an admin.
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login
            return redirect_to_login(request.get_full_path())
        
        if not request.user.is_admin:
            raise PermissionDenied
            
        return view_func(request, *args, **kwargs)
    return _wrapped_view

def clean_directory_value(value) -> str:
    """An LDAP attribute as text. A missing attribute reads as "[]" or "None": that is no value."""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ''
    text = '' if value is None else str(value).strip()
    return '' if text in ('[]', 'None') else text


def find_directory_user(username: str, email: str = '', ad2000: str = ''):
    """
    The local account of a person who just authenticated against the directory.

    The directory username is the identity. AD2000 is the authenticated account name itself (and is
    unique), so it is a safe fallback. E-mail is not: it is only used when it points at exactly one
    account that is not an administrator, so people who share a mailbox (or whose mail is missing)
    are never merged into one account.
    """
    from .models import CustomUser

    user = CustomUser.objects.filter(username__iexact=username).first() if username else None
    if user is not None:
        return user
    ad2000 = clean_directory_value(ad2000)
    if ad2000:
        user = CustomUser.objects.filter(ad2000__iexact=ad2000).first()
        if user is not None:
            return user
    email = clean_directory_value(email)
    if email:
        matches = list(CustomUser.objects.filter(email__iexact=email)[:2])
        if len(matches) == 1 and not matches[0].is_admin:
            return matches[0]
    return None


def is_admin_role(role) -> bool:
    from django.conf import settings
    return bool(role and role.name.lower() == settings.ADMIN_ROLE_NAME.lower())


def other_active_admins(user):
    """Active administrators other than ``user`` (superuser flag or admin role)."""
    from django.conf import settings
    from django.db.models import Q
    from .models import CustomUser

    return CustomUser.objects.filter(is_active=True).filter(
        Q(is_superuser=True) | Q(role__name__iexact=settings.ADMIN_ROLE_NAME)
    ).exclude(pk=user.pk)


def apply_role(user, role) -> None:
    """
    Give ``user`` a role (or none) and keep the admin flags in step with it, so that taking the admin
    role away also takes the superuser rights away. The caller saves.
    """
    admin = is_admin_role(role)
    user.role = role
    user.is_superuser = admin
    user.is_staff = admin


def log_history(user, action):
    """
    Logs user actions in the UserHistory model.
    """
    allowed_prefixes = (
        "Utilisateur connecté",
        "Rapport consulté",
        "Ticket créé",
    )
    if user.is_authenticated and action.startswith(allowed_prefixes):
        UserHistory.objects.create(user=user, action=action, timestamp=now())

def get_user_permissions(user) -> dict[str, bool]:
    """
    Retrieves all permissions for a given user and returns them in a dictionary.
    Considers: superuser status, role permissions, and direct user permissions.

    The result is kept on the user object, so the context processor and the
    views of one request share a single computation.
    """
    cached = getattr(user, '_cbi_permissions', None)
    if cached is None:
        cached = _compute_user_permissions(user)
        try:
            user._cbi_permissions = cached
        except AttributeError:  # lazy/anonymous user objects that refuse attributes
            pass
    return cached


def _compute_user_permissions(user) -> dict[str, bool]:
    all_permissions = [
        'add_permission', 'change_permission', 'delete_permission', 'view_permission',

        'add_notification', 'change_notification', 'delete_notification', 'view_notification',
        'add_powerbireport', 'change_powerbireport', 'delete_powerbireport', 'view_powerbireport',
        'add_report', 'change_report', 'delete_report', 'view_report',
        'add_reportaccess', 'change_reportaccess', 'delete_reportaccess', 'view_reportaccess',
        'add_task', 'change_task', 'delete_task', 'view_task',
        'view_dashboard',
        'add_customuser', 'change_customuser', 'delete_customuser', 'view_customuser',
        'add_role', 'change_role', 'delete_role', 'view_role',
        'add_userhistory', 'change_userhistory', 'delete_userhistory', 'view_userhistory',
        'can_view_direction', 'can_view_pole', 'can_view_consolide', 'can_view_anomalie', 'can_view_module',
    ]
    
    if not user.is_authenticated:
        return {perm: False for perm in all_permissions}
    
    # Initialize permissions dict
    if user.is_superuser:
        permissions = {perm: True for perm in all_permissions}
    else:
        # Collect permissions from role and direct user permissions
        user_perm_set = set(user.user_permissions.values_list('codename', flat=True))
        
        # Add role permissions if user has a role
        if user.role:
            role_perms = user.role.permissions.values_list('codename', flat=True)
            user_perm_set.update(role_perms)
        
        permissions = {perm: perm in user_perm_set for perm in all_permissions}
    
    # Custom boolean permissions from the user model
    if user.is_admin:
        permissions['can_view_direction'] = True
        permissions['can_view_pole'] = True
        permissions['can_view_consolide'] = True
        permissions['can_view_anomalie'] = True
        permissions['can_view_module'] = True
    else:
        permissions['can_view_direction'] = getattr(user, 'can_view_direction', False)
        permissions['can_view_pole'] = getattr(user, 'can_view_pole', False)
        permissions['can_view_consolide'] = getattr(user, 'can_view_consolide', False)
        permissions['can_view_anomalie'] = getattr(user, 'can_view_anomalie', False)
        permissions['can_view_module'] = getattr(user, 'can_view_module', True)
    
    return permissions



def person_label(value) -> str:
    """
    How a person is named in a notification: "Prénom Nom", never the AD identifier.
    Accepts a user object, or an AD identifier / username (looked up); falls back to what was given.
    """
    from django.db.models import Q
    from .models import CustomUser

    if isinstance(value, CustomUser):
        user = value
    else:
        text = (value or '').strip()
        if not text:
            return ''
        user = CustomUser.objects.filter(Q(ad2000__iexact=text) | Q(username__iexact=text)).first()
    if user is None:
        return str(value or '')
    return user.get_full_name() or user.username
