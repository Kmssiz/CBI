from functools import wraps
from django.core.exceptions import PermissionDenied
from django.conf import settings
from .models import UserHistory, CustomUser, Role
from django.utils.timezone import now
from notifications.models import Notification

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

def get_user_permissions(user):
    """
    Retrieves all permissions for a given user and returns them in a dictionary.
    Considers: superuser status, role permissions, and direct user permissions.
    """
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
        'can_view_direction', 'can_view_pole', 'can_view_consolide', 'can_view_anomalie',
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
    permissions['can_view_anomalie'] = getattr(user, 'can_view_anomalie', False)
    
    # Direction and Pôle are STRICTLY filtered by default_view for EVERYONE (including superusers)
    # This ensures the "Default View" setting actually works as expected in the UI
    permissions['can_view_direction'] = (user.default_view == 'direction')
    permissions['can_view_pole'] = (user.default_view == 'pole')
    
    # Admins/Superusers always have access to other functional sections
    if user.is_admin:
        permissions['can_view_consolide'] = True
        permissions['can_view_anomalie'] = True
    else:
        permissions['can_view_consolide'] = getattr(user, 'can_view_consolide', False)
        # can_view_anomalie is already set from model attribute or False at line 76
    
    return permissions

