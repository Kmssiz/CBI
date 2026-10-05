from django.urls import path

from . import auth, views

app_name = 'mobile'

urlpatterns = [
    path('config/', auth.config_view, name='config'),
    path('auth/login/', auth.login_view, name='login'),
    path('auth/logout/', auth.logout_view, name='logout'),

    path('me/', views.me_view, name='me'),
    path('me/photo/', views.me_photo_view, name='me_photo'),
    path('users/<int:user_id>/photo/', views.user_photo_view, name='user_photo'),

    path('catalog/', views.catalog_view, name='catalog'),
    path('metadata/<int:option_id>/logo/', views.metadata_logo_view, name='metadata_logo'),
    path('reports/<int:report_id>/', views.report_detail_view, name='report'),
    path('reports/<int:report_id>/open/', views.report_open_view, name='report_open'),
    path('reports/<int:report_id>/close/', views.report_close_view, name='report_close'),
    path('reports/<int:report_id>/mobile-layout/', views.report_mobile_layout_view, name='report_mobile_layout'),

    path('favorites/', views.favorites_view, name='favorites'),
    path('favorites/<int:report_id>/', views.favorite_view, name='favorite'),

    path('notifications/', views.notifications_view, name='notifications'),
    path('notifications/unread-count/', views.notifications_unread_count_view, name='notifications_unread_count'),
    path('notifications/read-all/', views.notifications_read_all_view, name='notifications_read_all'),
    path('notifications/<int:notification_id>/', views.notification_delete_view, name='notification'),
    path('notifications/<int:notification_id>/read/', views.notification_read_view, name='notification_read'),

    path('history/', views.history_view, name='history'),
    path('history/users/', views.history_users_view, name='history_users'),
    path('history/users/<int:user_id>/', views.history_user_detail_view, name='history_user'),

    path('tickets/', views.tickets_view, name='tickets'),
    path('tickets/<int:ticket_id>/', views.ticket_detail_view, name='ticket'),
    path('tickets/<int:ticket_id>/messages/', views.ticket_message_view, name='ticket_messages'),
]
