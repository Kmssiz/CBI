"""
Try the "new user" scenario without waiting for the hourly Odoo match and the daily digest.

  python manage.py simulate_new_user --username jdupont      flag a REAL account as a new user + notify every admin now
  python manage.py simulate_new_user --fake "Test Dupont"     create a harmless fake person (screens only, see below)
  python manage.py simulate_new_user --cleanup                remove the fakes and every pending simulation alert

--username needs an account that already exists (the person logged in once). It resets the person's
"new user" alert (waiting again, not seen) and sends the notification, exactly as the digest would.

--fake creates "simulation.*" people who cannot log in. They exist only to see the notification and the
"Nouveaux utilisateurs" page. Do NOT grant them reports: PBIRS has no such account, so the grant would
write a policy for a person who does not exist. Use --username with a real test account for that.

Nothing here grants any access.
"""
import uuid

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from powerbi_report.models import NewUserAlert, OdooProfile
from users.models import CustomUser

FAKE_PREFIX = "simulation."


class Command(BaseCommand):
    help = "Simulate a new user arriving, so the admin notification and the review page can be tried."

    def add_arguments(self, parser):
        parser.add_argument("--username", help="An existing account to flag as a new user.")
        parser.add_argument("--fake", metavar="NAME", help="Create a fake person (screens only), e.g. \"Test Dupont\".")
        parser.add_argument("--cleanup", action="store_true", help="Remove the fake people and the pending simulation alerts.")
        parser.add_argument("--no-notify", action="store_true", help="Flag only; do not send the notification now.")

    def handle(self, *args, **options):
        chosen = [bool(options["username"]), bool(options["fake"]), options["cleanup"]]
        if sum(chosen) != 1:
            raise CommandError("Use exactly one of --username, --fake or --cleanup (see --help).")

        if options["cleanup"]:
            fakes = CustomUser.objects.filter(username__startswith=FAKE_PREFIX)
            count = fakes.count()
            fakes.delete()                                     # their alerts and notifications go with them
            self.stdout.write(f"{count} fake person(s) removed.")
            return

        if options["username"]:
            user = CustomUser.objects.filter(username__iexact=options["username"].strip()).first()
            if user is None:
                raise CommandError(
                    f"No account '{options['username']}'. The person must log in once first: that is what creates the account."
                )
            self._flag(user)
            self.stdout.write(f"{user.username} is now waiting on the 'Nouveaux utilisateurs' page.")
        else:
            user = self._create_fake(options["fake"].strip())
            self.stdout.write(f"Fake person '{user.get_full_name()}' ({user.username}) created and waiting.")
            self.stdout.write(self.style.WARNING("Screens only: do not grant this person any report."))

        if not options["no_notify"]:
            call_command("new_users_digest", force=True, stdout=self.stdout)

    def _flag(self, user):
        alert, _ = NewUserAlert.objects.get_or_create(user=user)
        NewUserAlert.objects.filter(pk=alert.pk).update(seen_at=None, seen_by=None, detected_at=timezone.now())

    def _create_fake(self, name):
        if not name:
            raise CommandError("Give the fake person a name, e.g. --fake \"Test Dupont\".")
        first, _, last = name.partition(" ")
        ident = uuid.uuid4().hex[:8]
        user = CustomUser(
            username=f"{FAKE_PREFIX}{ident}", first_name=first, last_name=last or "Simulation",
            email="", ad2000=f"sim{ident}", status="Active",
        )
        user.set_unusable_password()
        user.save()
        OdooProfile.objects.create(user=user, employee_id=f"sim{ident}", title="Poste de simulation",
                                   title_key="poste de simulation", company="Société de test", company_key="societe de test")
        self._flag(user)
        return user
