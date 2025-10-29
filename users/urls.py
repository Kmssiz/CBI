from django.urls import path
from django.contrib.auth.views import LogoutView
from .views import (
    login_view, 
    home_view, 
    logout_view, 
    user_management, 
    profile, 
    edit_profile, 
    user_details, 
    sync_users,
    user_edit,
    user_history,
    clear_history,
    manage_roles,
    create_role,
    edit_role,
    remove_role,
    permissions_list
)
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('', login_view, name='login'),
    path('logout/', logout_view, name='logout'),
    path('home/', home_view, name='home'),
    path('users_view/', user_management, name='users_view'),
    path('profile/', profile, name='profile'),
    path('user_details/', user_details, name='user_details'),
    path('edit-profile/', edit_profile, name='edit_profile'),
    path('sync_users/', sync_users, name='sync_users'),
    path('edit_user/<int:user_id>/', user_edit, name='user_edit'),
    path('user-history/<int:user_id>/', user_history, name='user_history'),
    path('clear-history/<int:user_id>/', clear_history, name='clear_history'),
    path("manage-roles/", manage_roles, name="manage_roles"),
    path('create_role/', create_role, name='create_role'),
    path("roles/edit/<int:role_id>/", edit_role, name="edit_role"),
    path("roles/remove/<int:role_id>/", remove_role, name="remove_role"),
    path('roles/permissions/<int:role_id>/', permissions_list, name='permissions_list'),


] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
