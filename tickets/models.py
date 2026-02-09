from django.db import models
from django.conf import settings

class Ticket(models.Model):
    TICKET_TYPE_CHOICES = (
        ('bug', 'Bug Report'),
        ('dashboard', 'Dashboard Request'),
        ('refresh', 'Refresh Request'),
        ('other', 'Other'),
    )

    STATUS_CHOICES = (
        ('open', 'Open'),
        ('in_progress', 'In Progress'),
        ('closed', 'Closed'),
        ('rejected', 'Rejected'),
    )

    CATEGORY_CHOICES = (
        ('bibliotheque', 'Bibliothèque'),
        ('cbi', 'CBI'),
    )

    title = models.CharField(max_length=200)
    description = models.TextField()
    ticket_type = models.CharField(max_length=20, choices=TICKET_TYPE_CHOICES)
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='cbi')
    priority = models.CharField(max_length=20, choices=[('low', 'Low'), ('medium', 'Medium'), ('high', 'High')], default='medium')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='open')
    
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='tickets_created')
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='tickets_assigned')
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"[{self.get_ticket_type_display()}] {self.title}"

    class Meta:
        ordering = ['-created_at']
