from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import models
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from .forms import TicketForm, TicketMessageForm
from .models import Ticket


def _is_admin(user) -> bool:
    role_name = (getattr(getattr(user, "role", None), "name", "") or "").lower()
    return (
        user.is_superuser
        or role_name == settings.ADMIN_ROLE_NAME.lower()
        or user.groups.filter(name=settings.ADMIN_ROLE_NAME).exists()
    )


def _can_access_ticket(user, ticket: Ticket) -> bool:
    return _is_admin(user) or ticket.created_by_id == user.id or ticket.assigned_to_id == user.id


def _get_admin_users():
    user_model = get_user_model()
    return user_model.objects.filter(
        models.Q(is_superuser=True)
        | models.Q(role__name__iexact=settings.ADMIN_ROLE_NAME)
        | models.Q(groups__name=settings.ADMIN_ROLE_NAME)
    ).distinct()


def _save_ticket_message(request: HttpRequest, ticket: Ticket, form: TicketMessageForm) -> bool:
    if not form.is_valid():
        return False

    ticket_message = form.save(commit=False)
    ticket_message.ticket = ticket
    ticket_message.sender = request.user
    ticket_message.save()
    messages.success(request, "Message envoye.")
    return True


def _render_ticket_detail(
    request: HttpRequest, ticket: Ticket, message_form: TicketMessageForm | None = None
) -> HttpResponse:
    context = {
        "ticket": ticket,
        "admins": _get_admin_users(),
        "ticket_messages": ticket.messages.select_related("sender"),
        "message_form": message_form or TicketMessageForm(),
        "is_admin": _is_admin(request.user),
    }
    return render(request, "tickets/ticket_detail.html", context)


@login_required
def ticket_list(request):
    if _is_admin(request.user):
        tickets = Ticket.objects.all()
    else:
        tickets = Ticket.objects.filter(created_by=request.user)

    return render(request, "tickets/ticket_list.html", {"tickets": tickets})


@login_required
def create_ticket(request):
    if request.method == "POST":
        form = TicketForm(request.POST, request.FILES)
        if form.is_valid():
            ticket = form.save(commit=False)
            ticket.created_by = request.user
            ticket.save()
            messages.success(request, "Ticket cree avec succes.")
            return redirect("tickets:ticket_list")
    else:
        form = TicketForm()

    return render(request, "tickets/create_ticket.html", {"form": form})


@login_required
def ticket_detail(request, ticket_id):
    ticket = get_object_or_404(Ticket, id=ticket_id)

    if not _can_access_ticket(request.user, ticket):
        messages.error(request, "Vous n'avez pas la permission de voir ce ticket.")
        return redirect("tickets:ticket_list")

    if request.method == "POST":
        message_form = TicketMessageForm(request.POST)
        if _save_ticket_message(request, ticket, message_form):
            return redirect("tickets:ticket_detail", ticket_id=ticket.id)

        messages.error(request, "Le message n'a pas pu etre envoye.")
        return _render_ticket_detail(request, ticket, message_form)

    return _render_ticket_detail(request, ticket)


@login_required
def assign_ticket(request, ticket_id):
    if not _is_admin(request.user):
        messages.error(request, "Vous n'avez pas la permission d'effectuer cette action.")
        return redirect("tickets:ticket_list")

    ticket = get_object_or_404(Ticket, id=ticket_id)

    if request.method == "POST":
        user_id = request.POST.get("assigned_to")
        if user_id:
            try:
                user = _get_admin_users().get(id=user_id)
                ticket.assigned_to = user
                ticket.save()
                messages.success(
                    request, f"Ticket assigne a {user.get_full_name() or user.username}."
                )
            except get_user_model().DoesNotExist:
                messages.error(request, "Utilisateur introuvable.")
        else:
            ticket.assigned_to = None
            ticket.save()
            messages.success(request, "Ticket desassigne.")

    return redirect("tickets:ticket_detail", ticket_id=ticket.id)


@login_required
def change_status(request, ticket_id):
    if not _is_admin(request.user):
        messages.error(request, "Vous n'avez pas la permission d'effectuer cette action.")
        return redirect("tickets:ticket_list")

    ticket = get_object_or_404(Ticket, id=ticket_id)

    if request.method == "POST":
        new_status = request.POST.get("status")
        valid_statuses = [status[0] for status in Ticket.STATUS_CHOICES]
        if new_status in valid_statuses:
            ticket.status = new_status
            ticket.save()
            messages.success(
                request, f"Statut du ticket mis a jour : {ticket.get_status_display()}."
            )
        else:
            messages.error(request, "Statut invalide.")

    return redirect("tickets:ticket_detail", ticket_id=ticket.id)


@login_required
def update_ticket_info(request, ticket_id):
    if not _is_admin(request.user):
        messages.error(request, "Vous n'avez pas la permission d'effectuer cette action.")
        return redirect("tickets:ticket_list")

    ticket = get_object_or_404(Ticket, id=ticket_id)

    if request.method == "POST":
        new_status = request.POST.get("status")
        user_id = request.POST.get("assigned_to")

        valid_statuses = [status[0] for status in Ticket.STATUS_CHOICES]
        if new_status not in valid_statuses:
            messages.error(request, "Statut invalide.")
            return redirect("tickets:ticket_detail", ticket_id=ticket.id)

        ticket.status = new_status

        if user_id:
            try:
                ticket.assigned_to = _get_admin_users().get(id=user_id)
            except get_user_model().DoesNotExist:
                messages.error(request, "Utilisateur introuvable.")
                return redirect("tickets:ticket_detail", ticket_id=ticket.id)
        else:
            ticket.assigned_to = None

        ticket.save()
        messages.success(request, "Informations du ticket mises a jour.")

    return redirect("tickets:ticket_detail", ticket_id=ticket.id)


@login_required
def add_message(request, ticket_id):
    ticket = get_object_or_404(Ticket, id=ticket_id)

    if not _can_access_ticket(request.user, ticket):
        messages.error(request, "Vous n'avez pas la permission d'effectuer cette action.")
        return redirect("tickets:ticket_list")

    if request.method == "POST":
        form = TicketMessageForm(request.POST)
        if _save_ticket_message(request, ticket, form):
            return redirect("tickets:ticket_detail", ticket_id=ticket.id)

        messages.error(request, "Le message n'a pas pu etre envoye.")
        return _render_ticket_detail(request, ticket, form)

    return redirect("tickets:ticket_detail", ticket_id=ticket.id)
