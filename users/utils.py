from .models import UserHistory, CustomUser, Role
from django.utils.timezone import now
from notifications.models import Notification

def log_history(user, action):
    """
    Logs user actions in the UserHistory model.
    """
    if user.is_authenticated:
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
        'view_refresh',
        'add_reportaccess', 'change_reportaccess', 'delete_reportaccess', 'view_reportaccess',
        'add_task', 'change_task', 'delete_task', 'view_task',
        'view_dashboard',
        'add_customuser', 'change_customuser', 'delete_customuser', 'view_customuser',
        'add_role', 'change_role', 'delete_role', 'view_role',
        'add_userhistory', 'change_userhistory', 'delete_userhistory', 'view_userhistory',
    ]
    
    if not user.is_authenticated:
        return {perm: False for perm in all_permissions}
    
    # Superusers have all permissions
    if user.is_superuser:
        return {perm: True for perm in all_permissions}
    
    # Collect permissions from role and direct user permissions
    user_perm_set = set(user.user_permissions.values_list('codename', flat=True))
    
    # Add role permissions if user has a role
    if user.role:
        role_perms = user.role.permissions.values_list('codename', flat=True)
        user_perm_set.update(role_perms)
    
    permissions = {perm: perm in user_perm_set for perm in all_permissions}
    
    return permissions

