from django.urls import path
from . import views
from . import server_views

app_name = 'powerbi_report'

urlpatterns = [
    path('', views.report_list, name='report_list'),
    path('report/<str:report_id>/', views.report_detail, name='report_detail'),
    path('permissions/<str:report_id>/', views.report_permissions, name='report_permissions'),
    path('missing_users/<str:report_id>/', views.missing_users, name='missing_users'),

    path('user_permission/<str:username>/', views.user_permission, name='user_permission'),
    path('missing_permissions/<str:username>/', views.missing_permissions, name='missing_permissions'),
    path('remove_permission/<str:report_id>/<str:username>/', views.remove_permission_from_server, name='remove_permission_from_server'),
    path('remove_selected_permissions/<str:username>/', views.remove_selected_permissions, name='remove_selected_permissions'),
    path('remove_all_permissions/<str:username>/', views.remove_all_permissions, name='remove_all_permissions'),
    path('add_permission/<str:report_id>/<str:username>/', views.add_permission_to_server, name='add_permission_to_server'),


    path('report_list_flat/', views.report_list_flat, name='report_list_flat'),
    path('reports/hierarchy/', views.report_list_hierarchy, name='report_list_hierarchy'),
    path('reports/hierarchy/<path:folder_path>/', views.report_list_hierarchy, name='report_list_hierarchy_folder'),
    path('embed/<path:report_path>/', views.embed_report, name='embed_report'),

    path('add_all_permissions/<str:username>/', views.add_all_permissions, name='add_all_permissions'),
    path('add_selected_permissions/<str:username>/', views.add_selected_permissions, name='add_selected_permissions'),
    path('add_users/<str:report_id>/<str:username>/', views.add_users_to_report, name='add_users_to_report'),
    path('add_all_users/<str:report_id>/', views.add_all_users_to_report, name='add_all_users_to_report'),

    path('add-selected-users/<str:report_id>/', views.add_selected_users_to_report, name='add_selected_users_to_report'),
    path('remove-user/<str:report_id>/<str:username>/', views.remove_users_from_report, name='remove_user'),
    path('remove-selected-users/<str:report_id>/', views.remove_selected_users_from_report, name='remove_selected_users_from_report'),

    path('dashboard/', views.dashboard, name='dashboard'),
    path('no-reports/', views.users_no_reports_view, name='users_no_reports_view'),

    path('folders/', views.report_folders_list, name='report_folders_list'),
    path('folders/<path:folder_path>/', views.report_folders_list, name='report_folders_list_folder'),
    path('create-folder/', views.add_powerbi_folder, name='add_powerbi_folder'),
    path('delete_folder/<str:folder_id>/', views.delete_powerbi_folder, name='delete_powerbi_folder'),

    path('reports/edit/<str:report_id>/', views.edit_powerbi_report_name, name='edit_report'),
    path('reports/move/<str:report_id>/', views.edit_powerbi_report_path, name='edit_path'),
    path('reports/replace/<str:report_id>/', views.replace_powerbi_report, name='replace_report'),
    path('reports/delete/<str:report_id>/', views.delete_powerbi_report_server, name='delete_report_server'),
    path('report/<str:report_id>/edit-description/', views.edit_powerbi_report_description, name='edit_report_description'),

    # New: add report + local metadata management
    path('reports/add/', views.add_report_local, name='add_report_local'),
    path('report/<str:report_id>/metadata/', views.update_report_metadata_local, name='update_report_metadata'),

    path('load_cache/', views.load_cache, name='load_cache'),

    path('report/<str:report_id>/download/', views.download_report, name='download_report'),
    path('get_folders/', views.get_folders, name='get_folders'),

    path('report/<str:report_id>/add_refresh_plan/', views.add_refresh_plan, name='add_refresh_plan'),
    path('upload/', views.upload_powerbi_report, name='upload_powerbi_report'),

    path('folders/json/', views.get_folder_list, name='folder_list'),

    # Custom Virtual Folder Views (Local management)
    path('custom/direction/', views.custom_folders_list, {'view_type': 'direction'}, name='custom_direction'),
    path('custom/biblio/', views.custom_folders_list, {'view_type': 'biblio'}, name='custom_biblio'),
    path('custom/consolide/', views.custom_folders_list, {'view_type': 'consolide'}, name='custom_consolide'),
    path('custom/anomalie/', views.custom_folders_list, {'view_type': 'anomalie'}, name='custom_anomalie'),
    path('custom/pole/', views.custom_folders_list, {'view_type': 'pole'}, name='custom_pole'),
    
    path('custom/<str:view_type>/<int:folder_id>/', views.custom_folders_list, name='custom_folder_detail'),
    path('custom/folder/<str:view_type>/<int:folder_id>/report/<int:report_id>/', views.embed_custom_report, name='embed_custom_report'),

    # Custom Folder Management (Admin)
    path('custom/folder/create/<str:view_type>/', views.create_custom_folder, name='create_custom_folder'),
    path('custom/folder/<int:folder_id>/edit/', views.edit_custom_folder, name='edit_custom_folder'),
    path('custom/folder/move/<int:folder_id>/', views.move_custom_folder, name='move_custom_folder'),
    path('custom/folder/<int:folder_id>/delete/', views.delete_custom_folder, name='delete_custom_folder'),

    # Report Assignment (Admin)
    path('custom/folder/<int:folder_id>/assign/', views.assign_report_to_folder, name='assign_report_to_folder'),
    path('custom/folder/<int:folder_id>/remove/<int:report_id>/', views.remove_report_from_folder, name='remove_report_from_folder'),

    # Sync & API
    path('custom/sync/', views.sync_reports_from_pbirs, name='sync_reports'),
    path('custom/sync-permissions/', views.sync_permissions, name='sync_permissions'),
    path('custom/api/reports/', views.get_available_reports_json, name='available_reports_json'),
    path('refresh-history/<str:plan_id>/', views.get_refresh_plan_history, name='get_refresh_plan_history'),

    # PBIRS Server Management
    path('servers/', server_views.server_management_list, name='server_management_list'),
    path('servers/create/', server_views.server_create, name='server_create'),
    path('servers/edit/<int:server_id>/', server_views.server_edit, name='server_edit'),
    path('servers/delete/<int:server_id>/', server_views.server_delete, name='server_delete'),
]
