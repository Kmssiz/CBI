from django.core.management.base import BaseCommand
from django.urls import reverse
from django.utils import timezone

from notifications.models import Notification
from powerbi_report.models import NewUserAlert
from users.models import CustomUser

MARKER = "Nouveaux utilisateurs"


class Command(BaseCommand):
    help = (
        "Daily notification for admins: new users (in LDAP and Odoo) waiting to be reviewed. "
        "At most one per admin per day; nothing when there is nobody new."
    )

    def handle(self, *args, **options):
        waiting = NewUserAlert.objects.filter(seen_at__isnull=True).count()
        if not waiting:
            self.stdout.write("Nobody new.")
            return
        today = timezone.localdate()
        message = f"{MARKER} : {waiting} personne{'s' if waiting > 1 else ''} à examiner."
        link = reverse('new_users')

        sent = 0
        for admin in CustomUser.objects.filter(is_active=True):
            if not admin.is_admin:
                continue
            if Notification.objects.filter(user=admin, link=link, created_at__date=today).exists():
                continue
            Notification.objects.create(user=admin, message=message, link=link)
            sent += 1
        self.stdout.write(f"{sent} admin(s) notified.")
