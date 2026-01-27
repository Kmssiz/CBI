from django.urls import path
from . import views

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
    path('report/<str:report_id>/edit-description/', views.edit_powerbi_report_description, name='edit_report_description'),

    path('load_cache/', views.load_cache, name='load_cache'),
    path('report-security/<str:username>/<str:report_id>/', views.report_security_view, name='report_security'),


    path('report/<str:report_id>/download/', views.download_report, name='download_report'),
    path('get_folders/', views.get_folders, name='get_folders'),
    path('refresh-list/', views.get_report_refresh_list, name='get_report_refresh_list'),
    path('report/<str:report_id>/add_refresh_plan/', views.add_refresh_plan, name='add_refresh_plan'),
    path('upload/', views.upload_powerbi_report, name='upload_powerbi_report'),
    
    path('folders/', views.get_folder_list, name='folder_list'),

    # Custom Virtual Folder Views
    path('custom/business/', views.custom_folders_list, {'view_type': 'business'}, name='custom_business'),
    path('custom/business/<int:folder_id>/', views.custom_folders_list, {'view_type': 'business'}, name='custom_folder_detail'),
    # Department view shows PBIRS folders directly (same as Reports page)
    path('custom/department/', views.report_folders_list, name='custom_department'),
    path('custom/department/<path:folder_path>/', views.report_folders_list, name='custom_department_folder'),
    
    # Custom Folder Management (Admin)
    path('custom/folder/create/<str:view_type>/', views.create_custom_folder, name='create_custom_folder'),
    path('custom/folder/<int:folder_id>/edit/', views.edit_custom_folder, name='edit_custom_folder'),
    path('custom/folder/<int:folder_id>/delete/', views.delete_custom_folder, name='delete_custom_folder'),
    
    # Report Assignment (Admin)
    path('custom/folder/<int:folder_id>/assign/', views.assign_report_to_folder, name='assign_report_to_folder'),
    path('custom/folder/<int:folder_id>/remove/<int:report_id>/', views.remove_report_from_folder, name='remove_report_from_folder'),
    
    # Sync & API
    path('custom/sync/', views.sync_reports_from_pbirs, name='sync_reports'),
    path('custom/api/reports/', views.get_available_reports_json, name='available_reports_json'),
]
