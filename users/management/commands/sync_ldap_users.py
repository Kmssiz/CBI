from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from users.directory_sync import deactivate_missing_users, sync_directory_users
from users.ldap_utils import get_ad_users


class Command(BaseCommand):
    help = "Create the accounts of new AD users (service account). Run hourly by the sync container."

    def add_arguments(self, parser):
        parser.add_argument(
            "--deactivate-missing", action="store_true",
            default=getattr(settings, "LDAP_DEACTIVATE_MISSING", False),
            help="Also switch off accounts that AD no longer lists as active (LDAP_DEACTIVATE_MISSING).",
        )

    def handle(self, *args, **options):
        username, password = settings.LDAP_SERVICE_USERNAME, settings.LDAP_SERVICE_PASSWORD
        if not username or not password:
            raise CommandError("LDAP_SERVICE_USERNAME / LDAP_SERVICE_PASSWORD are not configured.")
        ldap_users = get_ad_users(username, password)
        if not ldap_users:
            raise CommandError("The directory returned no user: nothing was changed.")
        result = sync_directory_users(ldap_users)
        self.stdout.write(self.style.SUCCESS(
            f"{result.created} new accounts, {result.existing} already known, {result.skipped} skipped."
        ))
        if options["deactivate_missing"]:
            self.stdout.write(f"{deactivate_missing_users(ldap_users)} account(s) deactivated.")
