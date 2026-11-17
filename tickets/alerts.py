"""In-app alert (the bell) for the admins when a ticket is created."""
from django.urls import reverse

from notifications.models import Notification


def notify_admins_of_new_ticket(ticket) -> int:
    """One notification per admin, linking to the ticket. The person who opened it is not notified."""
    from tickets.views import _get_admin_users

    link = reverse("tickets:ticket_detail", args=[ticket.id])
    name = ticket.created_by.get_full_name() or ticket.created_by.username
    message = f"Nouveau ticket #{ticket.id} de {name} : {ticket.title}"
    admins = [a for a in _get_admin_users() if a.pk != ticket.created_by_id and a.is_active]
    Notification.objects.bulk_create(Notification(user=a, message=message, link=link) for a in admins)
    return len(admins)
