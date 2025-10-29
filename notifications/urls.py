from django.urls import path
from .views import notifications_list, delete_notification, delete_all_notifications, mark_notifications,mark_as_read

urlpatterns = [
    path('', notifications_list, name='notifications'),
    path('delete/<int:id>/', delete_notification, name='delete_notification'),
    path('delete_all/', delete_all_notifications, name='delete_all'),
    path('mark_notifications/', mark_notifications, name='mark_notifications'),
    path('mark_as_read/<int:notification_id>/', mark_as_read, name='mark_as_read'),

]

