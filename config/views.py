from django.http import HttpResponseServerError
from django.shortcuts import render
from django.template import loader

def custom_404_view(request, exception=None):
    return render(request, '404.html', status=404)

def custom_403_view(request, exception=None):
    return render(request, '403.html', status=403)

def custom_500_view(request):
    # No request context: context processors may hit the DB, which can be
    # the very thing that failed.
    return HttpResponseServerError(loader.get_template('500.html').render())
