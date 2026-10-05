"""Tickets for the mobile app: the platform's ticket system (tickets app), same rules.

Validation reuses ``tickets.forms`` (fields, choices, 5 MB image attachments) and
permissions reuse ``tickets.views`` helpers: admins see every ticket and can
change its status / assignee; other users see the tickets they created.
"""
import mimetypes

from django.contrib.auth import get_user_model
from django.db.models import Count
from django.http import FileResponse, HttpRequest, HttpResponse, JsonResponse
from django.urls import reverse

from tickets.forms import TicketForm, TicketMessageForm
from tickets.models import Ticket, TicketMessage
from tickets.views import _can_access_ticket, _get_admin_users, _is_admin
from users.models import CustomUser, UserHistory

from .http import api_error, dispatch, mobile_endpoint, read_json

STATUSES = [value for value, _ in Ticket.STATUS_CHOICES]


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _person(user: CustomUser | None) -> dict | None:
    if user is None:
        return None
    return {
        'id': user.pk,
        'name': user.get_full_name() or user.username,
        'initials': ''.join(char for char in user.get_initials() if char.isalnum()),
        'avatar_color': user.get_avatar_color(),
    }


def _choices(choices) -> list[dict]:
    return [{'value': value, 'label': label} for value, label in choices]


def serialize_ticket(ticket: Ticket, messages_count: int | None = None) -> dict:
    return {
        'id': ticket.pk,
        'title': ticket.title,
        'description': ticket.description,
        'ticket_type': ticket.ticket_type,
        'ticket_type_label': ticket.get_ticket_type_display(),
        'category': ticket.category,
        'category_label': ticket.get_category_display(),
        'priority': ticket.priority,
        'priority_label': ticket.get_priority_display(),
        'status': ticket.status,
        'status_label': ticket.get_status_display(),
        'created_by': _person(ticket.created_by),
        'assigned_to': _person(ticket.assigned_to),
        'attachment_url': reverse('mobile:ticket_attachment', args=[ticket.pk]) if ticket.attachment else None,
        'created_at': _iso(ticket.created_at),
        'updated_at': _iso(ticket.updated_at),
        'messages_count': messages_count if messages_count is not None else ticket.messages.count(),
    }


def serialize_message(message: TicketMessage, viewer: CustomUser) -> dict:
    return {
        'id': message.pk,
        'sender': message.sender.get_full_name() or message.sender.username,
        'author': _person(message.sender),
        'is_mine': message.sender_id == viewer.pk,
        'from_admin': _is_admin(message.sender),
        'content': message.content,
        'attachment_url': (
            reverse('mobile:ticket_message_attachment', args=[message.ticket_id, message.pk])
            if message.attachment else None
        ),
        'created_at': _iso(message.created_at),
    }


def _form_input(request: HttpRequest) -> tuple[dict | None, dict]:
    """JSON body, or multipart (fields + ``attachment`` file) as the web forms expect."""
    if request.content_type and request.content_type.startswith('multipart/'):
        return request.POST, request.FILES
    return read_json(request), {}


def _form_errors(form) -> JsonResponse:
    errors = {field: [str(error) for error in field_errors] for field, field_errors in form.errors.items()}
    first = next(iter(errors.values()), ['Formulaire invalide.'])[0]
    return JsonResponse({'detail': first, 'code': 'bad_request', 'errors': errors}, status=400)


def _accessible(request: HttpRequest, ticket_id: int) -> Ticket | None:
    ticket = Ticket.objects.select_related('created_by', 'assigned_to').filter(pk=ticket_id).first()
    return ticket if ticket and _can_access_ticket(request.user, ticket) else None


def _not_found() -> JsonResponse:
    return api_error(404, 'not_found', 'Ticket introuvable.')


# --- endpoints ---------------------------------------------------------------

@mobile_endpoint('GET')
def choices_view(request: HttpRequest) -> JsonResponse:
    return JsonResponse({
        'ticket_types': _choices(Ticket.TICKET_TYPE_CHOICES),
        'categories': _choices(Ticket.CATEGORY_CHOICES),
        'priorities': _choices(Ticket.PRIORITY_CHOICES),
        'statuses': _choices(Ticket.STATUS_CHOICES),
        'is_admin': _is_admin(request.user),
        'max_attachment_bytes': 5 * 1024 * 1024,
    })


def _list(request: HttpRequest) -> JsonResponse:
    admin = _is_admin(request.user)
    tickets = Ticket.objects.all() if admin else Ticket.objects.filter(created_by=request.user)
    status = request.GET.get('status', '').strip()
    if status in STATUSES:
        tickets = tickets.filter(status=status)
    if admin and request.GET.get('assigned') == 'me':
        tickets = tickets.filter(assigned_to=request.user)
    tickets = tickets.select_related('created_by', 'assigned_to').annotate(messages_total=Count('messages'))[:300]
    return JsonResponse({
        'is_admin': admin,
        'tickets': [serialize_ticket(ticket, ticket.messages_total) for ticket in tickets],
    })


def _create(request: HttpRequest) -> JsonResponse:
    data, files = _form_input(request)
    if data is None:
        return api_error(400, 'bad_request', 'Requête invalide.')
    data = data.copy() if hasattr(data, 'copy') else dict(data)
    data.setdefault('category', 'cbi')
    data.setdefault('priority', 'medium')
    form = TicketForm(data, files)
    if not form.is_valid():
        return _form_errors(form)
    ticket = form.save(commit=False)
    ticket.created_by = request.user
    ticket.save()
    UserHistory.objects.create(user=request.user, action=f'Ticket créé : {ticket.title}'[:255],
                               source=UserHistory.SOURCE_MOBILE)
    return JsonResponse(serialize_ticket(ticket, 0), status=201)


tickets_view = dispatch(GET=_list, POST=_create)


@mobile_endpoint('GET')
def ticket_detail_view(request: HttpRequest, ticket_id: int) -> JsonResponse:
    ticket = _accessible(request, ticket_id)
    if ticket is None:
        return _not_found()
    messages = list(ticket.messages.select_related('sender', 'sender__role'))
    admin = _is_admin(request.user)
    return JsonResponse({
        **serialize_ticket(ticket, len(messages)),
        'messages': [serialize_message(message, request.user) for message in messages],
        'can_manage': admin,
    })


@mobile_endpoint('POST')
def ticket_message_view(request: HttpRequest, ticket_id: int) -> JsonResponse:
    ticket = _accessible(request, ticket_id)
    if ticket is None:
        return _not_found()
    data, files = _form_input(request)
    if data is None:
        return api_error(400, 'bad_request', 'Requête invalide.')
    form = TicketMessageForm(data, files)
    if not form.is_valid():
        return _form_errors(form)
    message = form.save(commit=False)
    message.ticket = ticket
    message.sender = request.user
    message.save()
    ticket.save(update_fields=['updated_at'])
    return JsonResponse(serialize_message(message, request.user), status=201)


@mobile_endpoint('POST')
def ticket_update_view(request: HttpRequest, ticket_id: int) -> JsonResponse:
    """Status and assignee, admins only (web: tickets.views.update_ticket_info)."""
    if not _is_admin(request.user):
        return api_error(403, 'forbidden', "Vous n'avez pas la permission d'effectuer cette action.")
    ticket = _accessible(request, ticket_id)
    if ticket is None:
        return _not_found()
    data = read_json(request)
    if data is None:
        return api_error(400, 'bad_request', 'Requête invalide.')
    if 'status' in data:
        if data['status'] not in STATUSES:
            return api_error(400, 'bad_request', 'Statut invalide.')
        ticket.status = data['status']
    if 'assigned_to' in data:
        assignee = data['assigned_to']
        if assignee in (None, ''):
            ticket.assigned_to = None
        else:
            try:
                ticket.assigned_to = _get_admin_users().get(pk=int(assignee))
            except (TypeError, ValueError, get_user_model().DoesNotExist):
                return api_error(400, 'bad_request', 'Utilisateur introuvable.')
    ticket.save()
    return JsonResponse(serialize_ticket(ticket))


@mobile_endpoint('GET')
def ticket_admins_view(request: HttpRequest) -> JsonResponse:
    if not _is_admin(request.user):
        return api_error(403, 'forbidden', 'Réservé aux administrateurs.')
    admins = sorted(_get_admin_users(), key=lambda user: (user.get_full_name() or user.username).casefold())
    return JsonResponse({'admins': [_person(user) for user in admins]})


def _file(field) -> HttpResponse:
    if not field:
        return _not_found()
    try:
        handle = field.open('rb')
    except (FileNotFoundError, OSError):
        return _not_found()
    response = FileResponse(handle, content_type=mimetypes.guess_type(field.name)[0] or 'application/octet-stream')
    response['Cache-Control'] = 'private, max-age=86400'
    return response


@mobile_endpoint('GET')
def ticket_attachment_view(request: HttpRequest, ticket_id: int) -> HttpResponse:
    ticket = _accessible(request, ticket_id)
    return _file(ticket.attachment) if ticket else _not_found()


@mobile_endpoint('GET')
def ticket_message_attachment_view(request: HttpRequest, ticket_id: int, message_id: int) -> HttpResponse:
    ticket = _accessible(request, ticket_id)
    if ticket is None:
        return _not_found()
    message = ticket.messages.filter(pk=message_id).first()
    return _file(message.attachment) if message else _not_found()
