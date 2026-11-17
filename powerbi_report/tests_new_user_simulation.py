"""The 'new user' scenario can be tried on demand: flag a person, notify the admins, review, clean up."""
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from notifications.models import Notification
from powerbi_report.models import NewUserAlert, OdooProfile

User = get_user_model()
LINK = "/new-users/"


class SimulateNewUserTests(TestCase):
    def setUp(self):
        self.admin1 = User.objects.create_superuser(username="admin1", password="x")
        self.admin2 = User.objects.create_superuser(username="admin2", password="x")
        self.person = User.objects.create_user(username="jdupont", password="x", first_name="Jean", last_name="Dupont", ad2000="jd1")

    def run_cmd(self, **options):
        out = StringIO()
        call_command("simulate_new_user", stdout=out, **options)
        return out.getvalue()

    def notified(self):
        return sorted(Notification.objects.filter(link=LINK).values_list("user__username", flat=True))

    def test_a_real_account_is_flagged_and_every_admin_is_notified_at_once(self):
        self.run_cmd(username="jdupont")
        alert = NewUserAlert.objects.get(user=self.person)
        self.assertIsNone(alert.seen_at)
        self.assertEqual(self.notified(), ["admin1", "admin2"])
        self.assertIn("1 personne", Notification.objects.filter(link=LINK).first().message)

    def test_it_can_be_repeated_the_same_day(self):
        self.run_cmd(username="jdupont")
        self.run_cmd(username="jdupont")
        self.assertEqual(self.notified(), ["admin1", "admin1", "admin2", "admin2"])   # --force: a new notification each time

    def test_a_person_already_marked_as_seen_waits_again(self):
        NewUserAlert.objects.create(user=self.person, seen_at=timezone.now(), seen_by=self.admin1)
        self.run_cmd(username="jdupont", no_notify=True)
        alert = NewUserAlert.objects.get(user=self.person)
        self.assertIsNone(alert.seen_at)
        self.assertIsNone(alert.seen_by)

    def test_no_notify_only_flags(self):
        self.run_cmd(username="jdupont", no_notify=True)
        self.assertTrue(NewUserAlert.objects.filter(user=self.person).exists())
        self.assertEqual(self.notified(), [])

    def test_an_unknown_account_explains_what_to_do(self):
        with self.assertRaisesMessage(CommandError, "log in once"):
            self.run_cmd(username="nobody")

    def test_exactly_one_action_is_required(self):
        for options in ({}, {"username": "jdupont", "cleanup": True}, {"fake": "A B", "cleanup": True}):
            with self.subTest(options):
                with self.assertRaises(CommandError):
                    self.run_cmd(**options)

    def test_a_fake_person_shows_up_but_cannot_log_in(self):
        output = self.run_cmd(fake="Test Dupont")
        fake = User.objects.get(username__startswith="simulation.")
        self.assertEqual((fake.first_name, fake.last_name), ("Test", "Dupont"))
        self.assertFalse(fake.has_usable_password())
        self.assertTrue(OdooProfile.objects.filter(user=fake).exists())
        self.assertTrue(NewUserAlert.objects.filter(user=fake, seen_at__isnull=True).exists())
        self.assertIn("do not grant", output)
        self.assertEqual(self.notified(), ["admin1", "admin2"])
        self.client.force_login(self.admin1)
        page = self.client.get(reverse("new_users")).content.decode()
        self.assertIn("Test Dupont", page)

    def test_cleanup_removes_the_fakes_and_leaves_real_people_alone(self):
        self.run_cmd(fake="Test Un", no_notify=True)
        self.run_cmd(fake="Test Deux", no_notify=True)
        self.run_cmd(username="jdupont", no_notify=True)
        self.assertIn("2 fake person(s) removed", self.run_cmd(cleanup=True))
        self.assertFalse(User.objects.filter(username__startswith="simulation.").exists())
        self.assertTrue(User.objects.filter(username="jdupont").exists())
        self.assertTrue(NewUserAlert.objects.filter(user=self.person).exists())
        self.assertEqual(NewUserAlert.objects.count(), 1)

    def test_nothing_is_granted_by_the_simulation(self):
        from powerbi_report.models import UserReportPermission
        self.run_cmd(username="jdupont")
        self.run_cmd(fake="Test Dupont")
        self.assertEqual(UserReportPermission.objects.count(), 0)


class DigestForceTests(TestCase):
    def test_force_notifies_again_on_the_same_day_but_the_default_does_not(self):
        admin = User.objects.create_superuser(username="boss", password="x")
        person = User.objects.create_user(username="newbie", password="x")
        NewUserAlert.objects.create(user=person)
        call_command("new_users_digest", stdout=StringIO())
        call_command("new_users_digest", stdout=StringIO())
        self.assertEqual(Notification.objects.filter(user=admin, link=LINK).count(), 1)
        call_command("new_users_digest", force=True, stdout=StringIO())
        self.assertEqual(Notification.objects.filter(user=admin, link=LINK).count(), 2)
