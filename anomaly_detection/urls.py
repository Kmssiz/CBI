'''
from django.urls import path
from . import views

app_name = 'anomaly_detection'

urlpatterns = [
    path('', views.upload_log, name='upload_log'),
    path('results/', views.show_results, name='show_results'),
]


'''

from django.urls import path
from . import views  

app_name = 'anomaly_detection'  

urlpatterns = [
    path('upload_log/', views.upload_log, name='upload_log'),
    path('results/', views.results, name='results'),
    path('process_next_chunk/', views.process_next_chunk, name='process_next_chunk'),

]

