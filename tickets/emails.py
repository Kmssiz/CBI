"""
E-mails of the ticket system. Whoever is on the other side of a ticket hears about it:

  * the person who opened the ticket (the requester), and
  * the people handling it: the assigned admin, or every admin while nobody is assigned.

The person who just did something is never e-mailed about their own action, each
recipient gets their own message (addresses are never shared), and a mail problem
(SMTP down, bad address) is logged but never breaks the request that triggered it.
"""

import logging

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.urls import reverse

logger = logging.getLogger('tickets')


def _admins():
    from tickets.views import _get_admin_users
    return list(_get_admin_users())


def handlers(ticket) -> list:
    """The assigned admin, or all admins when the ticket is not assigned yet."""
    return [ticket.assigned_to] if ticket.assigned_to_id else _admins()


def _ticket_url(ticket) -> str:
    base = (getattr(settings, 'SITE_URL', '') or '').rstrip('/')
    return f"{base}{reverse('tickets:ticket_detail', args=[ticket.id])}"


def _name(user) -> str:
    return user.get_full_name() or user.username


def _send(recipients, subject: str, body: str, exclude=None) -> int:
    """One e-mail per distinct recipient with an address, skipping `exclude`. Returns how many were sent."""
    seen, targets = set(), []
    for user in recipients:
        if user is None or not user.email or user.pk == getattr(exclude, 'pk', None) or user.pk in seen:
            continue
        seen.add(user.pk)
        targets.append(user)
    if not targets:
        return 0
    sent = 0
    try:
        connection = get_connection(fail_silently=False)
        for user in targets:
            EmailMessage(
                subject=subject, body=body.replace('{name}', _name(user)),
                from_email=settings.DEFAULT_FROM_EMAIL, to=[user.email], connection=connection,
            ).send()
            sent += 1
    except Exception:  # SMTP down, refused recipient...: never break the ticket action
        logger.exception("Ticket e-mail not sent (%s)", subject)
    return sent


def _footer(ticket) -> str:
    return f"\nOuvrir le ticket : {_ticket_url(ticket)}\n\n— CBI, Groupe Hasnaoui"


def ticket_created(ticket) -> int:
    subject = f"[CBI] Ticket #{ticket.id} : {ticket.title}"
    mine = _send(
        [ticket.created_by], subject,
        "Bonjour {name},\n\nVotre ticket a bien été créé. Nous reviendrons vers vous dès que possible.\n\n"
        f"Type : {ticket.get_ticket_type_display()}\nPriorité : {ticket.get_priority_display()}\n"
        f"Titre : {ticket.title}\n" + _footer(ticket),
    )
    theirs = _send(
        handlers(ticket), f"[CBI] Nouveau ticket #{ticket.id} : {ticket.title}",
        "Bonjour {name},\n\n"
        f"{_name(ticket.created_by)} a ouvert un ticket.\n\n"
        f"Type : {ticket.get_ticket_type_display()}\nPriorité : {ticket.get_priority_display()}\n"
        f"Titre : {ticket.title}\n\n{ticket.description}\n" + _footer(ticket),
        exclude=ticket.created_by,
    )
    return mine + theirs


def message_posted(ticket, message) -> int:
    """A reply from the requester goes to the handlers; a reply from a handler goes to the requester."""
    from_requester = message.sender_id == ticket.created_by_id
    recipients = handlers(ticket) if from_requester else [ticket.created_by]
    return _send(
        recipients, f"[CBI] Nouveau message sur le ticket #{ticket.id} : {ticket.title}",
        "Bonjour {name},\n\n"
        f"{_name(message.sender)} a écrit sur le ticket « {ticket.title} » :\n\n{message.content}\n" + _footer(ticket),
        exclude=message.sender,
    )


def status_changed(ticket, actor) -> int:
    return _send(
        [ticket.created_by], f"[CBI] Ticket #{ticket.id} : {ticket.get_status_display()}",
        "Bonjour {name},\n\n"
        f"Le statut de votre ticket « {ticket.title} » est maintenant : {ticket.get_status_display()}.\n" + _footer(ticket),
        exclude=actor,
    )


def ticket_assigned(ticket, actor) -> int:
    if not ticket.assigned_to_id:
        return 0
    return _send(
        [ticket.assigned_to], f"[CBI] Ticket #{ticket.id} vous est assigné : {ticket.title}",
        "Bonjour {name},\n\n"
        f"Le ticket « {ticket.title} » de {_name(ticket.created_by)} vous a été assigné.\n" + _footer(ticket),
        exclude=actor,
    )
