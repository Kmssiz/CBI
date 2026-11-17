"""
E-mails of the ticket system. Whoever is on the other side of a ticket hears about it:

  * the person who opened the ticket (the requester), once an admin answers or changes its status, and
  * the people handling it: the assigned admin, or every admin while nobody is assigned.

A NEW ticket goes to every admin; the requester gets no confirmation mail for their own ticket.

The person who just did something is not e-mailed about their own action (except where a
caller asks for it), each recipient gets their own message (addresses are never shared), and
a mail problem (SMTP down, bad address) is logged but never breaks the request that triggered it.

Every message is multipart: a plain-text version and an HTML version styled like the portal
(templates/tickets/email/ticket_mail.html, inline CSS and no remote images, so it renders the
same in Outlook and Gmail).
"""

import logging
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.template.loader import render_to_string
from django.urls import reverse

logger = logging.getLogger('tickets')

# (background, text) of the small coloured labels in the mail.
BADGES = {
    'priority': {
        'low': ('#edf6d9', '#5f8220'),
        'medium': ('#fff4d6', '#8a5a00'),
        'high': ('#fde6e6', '#b42318'),
    },
    'status': {
        'open': ('#e7f5fb', '#0f6f99'),
        'in_progress': ('#fff4d6', '#8a5a00'),
        'closed': ('#edf6d9', '#5f8220'),
        'rejected': ('#fde6e6', '#b42318'),
    },
}


@dataclass
class Mail:
    subject: str
    heading: str
    intro: str
    rows: List[dict] = field(default_factory=list)
    quote: Optional[Tuple[str, str]] = None   # (author, text)
    preheader: str = ''


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


def _row(label: str, value: str, kind: str = '', key: str = '') -> dict:
    return {'label': label, 'value': value, 'badge': BADGES.get(kind, {}).get(key)}


def _details(ticket, with_type: bool = True) -> List[dict]:
    rows = [_row('Titre', ticket.title)]
    if with_type:
        rows.append(_row('Type', ticket.get_ticket_type_display()))
    rows.append(_row('Priorité', ticket.get_priority_display(), 'priority', ticket.priority))
    rows.append(_row('Statut', ticket.get_status_display(), 'status', ticket.status))
    return rows


def _plain(mail: Mail, name: str, url: str) -> str:
    lines = [f"Bonjour {name},", "", mail.heading, mail.intro, ""]
    lines += [f"{row['label']} : {row['value']}" for row in mail.rows]
    if mail.quote:
        lines += ["", f"{mail.quote[0]} :", mail.quote[1]]
    lines += ["", f"Ouvrir le ticket : {url}", "", "— CBI, Groupe Hasnaoui"]
    return "\n".join(lines)


def _send(recipients, mail: Mail, ticket, exclude=None) -> int:
    """One e-mail per distinct recipient with an address, skipping `exclude`. Returns how many were sent."""
    seen, targets = set(), []
    for user in recipients:
        if user is None or not user.email or user.pk == getattr(exclude, 'pk', None) or user.pk in seen:
            continue
        seen.add(user.pk)
        targets.append(user)
    if not targets:
        return 0

    url = _ticket_url(ticket)
    # Everything that touches the database or the templates is done here, in the request;
    # only the network part (SMTP) can be handed to a background thread.
    messages = []
    try:
        for user in targets:
            name = _name(user)
            message = EmailMultiAlternatives(
                subject=mail.subject, body=_plain(mail, name, url),
                from_email=settings.DEFAULT_FROM_EMAIL, to=[user.email],
            )
            message.attach_alternative(render_to_string('tickets/email/ticket_mail.html', {
                'subject': mail.subject, 'preheader': mail.preheader or mail.intro, 'name': name,
                'heading': mail.heading, 'intro': mail.intro, 'rows': mail.rows,
                'quote': {'author': mail.quote[0], 'text': mail.quote[1]} if mail.quote else None,
                'ticket_url': url, 'ticket_id': ticket.id,
            }), 'text/html')
            messages.append(message)
    except Exception:  # template problem...: never break the ticket action
        logger.exception("Ticket e-mail not built (%s)", mail.subject)
        return 0

    def deliver() -> int:
        sent = 0
        try:
            connection = get_connection(fail_silently=False)
        except Exception:  # SMTP misconfigured: never break the ticket action
            logger.exception("Ticket e-mail connection failed (%s)", mail.subject)
            return 0
        for message in messages:
            # One refused address must not stop the mail to the others.
            try:
                message.connection = connection
                message.send()
                sent += 1
            except Exception:
                logger.exception("Ticket e-mail not sent to %s (%s)", message.to, mail.subject)
        return sent

    if getattr(settings, 'TICKET_EMAILS_IN_BACKGROUND', False):
        threading.Thread(target=deliver, name='ticket-emails', daemon=True).start()
        return len(messages)
    return deliver()


def ticket_created(ticket) -> int:
    """E-mail the new ticket to EVERY admin (an admin who opened it themselves is not mailed about it)."""
    return _send(
        _admins(),
        Mail(
            subject=f"[CBI] Nouveau ticket #{ticket.id} : {ticket.title}",
            heading="Nouveau ticket",
            intro=f"{_name(ticket.created_by)} a ouvert un ticket.",
            rows=_details(ticket),
            quote=(_name(ticket.created_by), ticket.description),
        ),
        ticket, exclude=ticket.created_by,
    )


def message_posted(ticket, message) -> int:
    """A reply from the requester goes to the handlers; a reply from a handler goes to the requester."""
    from_requester = message.sender_id == ticket.created_by_id
    recipients = handlers(ticket) if from_requester else [ticket.created_by]
    return _send(
        recipients,
        Mail(
            subject=f"[CBI] Nouveau message sur le ticket #{ticket.id} : {ticket.title}",
            heading="Nouveau message",
            intro=f"{_name(message.sender)} a écrit sur le ticket « {ticket.title} ».",
            rows=_details(ticket, with_type=False),
            quote=(_name(message.sender), message.content),
        ),
        ticket, exclude=message.sender,
    )


def status_changed(ticket, actor) -> int:
    return _send(
        [ticket.created_by],
        Mail(
            subject=f"[CBI] Ticket #{ticket.id} : {ticket.get_status_display()}",
            heading="Le statut de votre ticket a changé",
            intro=f"Le statut de votre ticket « {ticket.title} » est maintenant : {ticket.get_status_display()}.",
            rows=_details(ticket, with_type=False),
        ),
        ticket, exclude=actor,
    )


def ticket_assigned(ticket, actor, include_actor: bool = False) -> int:
    """Tell the assigned admin. `include_actor` also mails them when they made the change themselves."""
    if not ticket.assigned_to_id:
        return 0
    return _send(
        [ticket.assigned_to],
        Mail(
            subject=f"[CBI] Ticket #{ticket.id} vous est assigné : {ticket.title}",
            heading="Un ticket vous est assigné",
            intro=f"Le ticket « {ticket.title} » de {_name(ticket.created_by)} vous a été assigné.",
            rows=_details(ticket),
            quote=(_name(ticket.created_by), ticket.description),
        ),
        ticket, exclude=None if include_actor else actor,
    )


def assignee_ticket_updated(ticket, actor, include_actor: bool = False) -> int:
    """The ticket an admin is handling changed status: tell that admin."""
    if not ticket.assigned_to_id:
        return 0
    return _send(
        [ticket.assigned_to],
        Mail(
            subject=f"[CBI] Ticket #{ticket.id} mis à jour : {ticket.get_status_display()}",
            heading="Un ticket qui vous est assigné a été mis à jour",
            intro=f"{_name(actor)} a mis à jour le ticket « {ticket.title} » : statut {ticket.get_status_display()}.",
            rows=_details(ticket),
        ),
        ticket, exclude=None if include_actor else actor,
    )
