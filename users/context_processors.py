from .utils import get_user_permissions

def user_permissions(request):
    """
    Context processor to add user permissions to the global context.
    """
    if request.user.is_authenticated:
        return {'permissions': get_user_permissions(request.user)}
    return {'permissions': {}}
