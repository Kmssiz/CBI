from django.conf import settings
from django.db import models


class Notification(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    message = models.TextField()
    link = models.CharField(max_length=300, blank=True, default='', help_text="Page ouverte au clic (facultatif)")
    created_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)

    class Meta:
        indexes = [
            models.Index(fields=['user', '-created_at'], name='notif_user_created_idx'),
            models.Index(fields=['user', 'is_read'], name='notif_user_read_idx'),
        ]

    def __str__(self):
        return f'Notification for {self.user.username}: {self.message}'