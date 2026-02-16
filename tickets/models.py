from django.conf import settings
from django.db import models


class Ticket(models.Model):
    TICKET_TYPE_CHOICES = (
        ("bug", "Signalement de bug"),
        ("dashboard", "Demande de dashboard"),
        ("refresh", "Demande d'actualisation"),
        ("other", "Autre"),
    )

    STATUS_CHOICES = (
        ("open", "Ouvert"),
        ("in_progress", "En cours"),
        ("closed", "Ferme"),
        ("rejected", "Rejete"),
    )

    CATEGORY_CHOICES = (
        ("bibliotheque", "Bibliotheque"),
        ("cbi", "CBI"),
    )

    PRIORITY_CHOICES = (
        ("low", "Basse"),
        ("medium", "Moyenne"),
        ("high", "Haute"),
    )

    title = models.CharField(max_length=200)
    description = models.TextField()
    ticket_type = models.CharField(max_length=20, choices=TICKET_TYPE_CHOICES)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default="cbi")
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default="medium")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open")
    attachment = models.ImageField(upload_to="ticket_attachments/", null=True, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="tickets_created",
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tickets_assigned",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"[{self.get_ticket_type_display()}] {self.title}"

    class Meta:
        ordering = ["-created_at"]


class TicketMessage(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"Message de {self.sender} sur {self.ticket}"
