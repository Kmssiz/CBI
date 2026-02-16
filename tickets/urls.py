from django.urls import path
from . import views

app_name = 'tickets'

urlpatterns = [
    path('', views.ticket_list, name='ticket_list'),
    path('create/', views.create_ticket, name='create_ticket'),
    path('<int:ticket_id>/', views.ticket_detail, name='ticket_detail'),
    path('<int:ticket_id>/assign/', views.assign_ticket, name='assign_ticket'),
    path('<int:ticket_id>/status/', views.change_status, name='change_status'),
    path('<int:ticket_id>/update/', views.update_ticket_info, name='update_ticket_info'),
    path('<int:ticket_id>/message/', views.add_message, name='add_message'),
]
