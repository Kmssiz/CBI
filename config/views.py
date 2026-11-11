from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404, HttpResponseServerError
from django.shortcuts import render
from django.template import loader
from django.views.static import serve

def custom_404_view(request, exception=None):
    return render(request, '404.html', status=404)

def custom_403_view(request, exception=None):
    return render(request, '403.html', status=403)

def custom_500_view(request):
    # No request context: context processors may hit the DB, which can be
    # the very thing that failed.
    return HttpResponseServerError(loader.get_template('500.html').render())


@login_required
def protected_media(request, path):
    """
    Serve uploaded files to signed-in users only.

    Nothing serves ``/media/`` in production, and a public mount would expose
    ticket attachments. Attachments are limited to the people on the ticket
    (and admins); profile photos and logos are visible to any signed-in user.
    """
    from tickets.models import Ticket

    user = request.user
    if path.startswith('ticket_attachments/'):
        tickets = Ticket.objects.filter(attachment=path)
    elif path.startswith('ticket_messages_attachments/'):
        tickets = Ticket.objects.filter(messages__attachment=path)
    else:
        tickets = None
    if tickets is not None and not user.is_admin:
        if not tickets.filter(Q(created_by=user) | Q(assigned_to=user)).exists():
            raise Http404
    return serve(request, path, document_root=settings.MEDIA_ROOT)
