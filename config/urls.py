"""Root URL configuration."""
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.generic import RedirectView
from django.conf import settings
from config.views import custom_404_view, custom_403_view, custom_500_view, protected_media

urlpatterns = [
    path('mobile/v1/', include('mobile.urls')),
    path('biadmin/', admin.site.urls),
    path('', include('users.urls')),
    # Compatibility route for the old dashboard URL.
    path('dashboard/', RedirectView.as_view(pattern_name='powerbi_report:dashboard', permanent=False)),
    path('notifications/', include('notifications.urls')),
    path('powerbi/', include('powerbi_report.urls', namespace='powerbi_report')),
    path('tickets/', include('tickets.urls')),
]

# Uploads are served by an authenticated view (see protected_media).
urlpatterns += [
    re_path(r'^' + settings.MEDIA_URL.lstrip('/') + r'(?P<path>.+)$', protected_media, name='media'),
]

handler404 = custom_404_view
handler403 = custom_403_view
handler500 = custom_500_view
