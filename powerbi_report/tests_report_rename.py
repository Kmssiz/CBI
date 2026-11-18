"""Renaming a report on the platform changes the name in CBI only; PBIRS is never called."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from notifications.models import Notification
from powerbi_report.models import ReportRef

User = get_user_model()


class ReportRenameTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username="boss", password="x", ad2000="boss1")
        self.other_admin = User.objects.create_superuser(username="boss2", password="x", ad2000="boss2")
        self.client.force_login(self.admin)
        self.ref = ReportRef.objects.create(pbirs_id="r-1", name="Ancien nom", path="/BI/Ancien nom")
        self.url = reverse("powerbi_report:edit_report", args=["r-1"])
        # Any HTTP call to the report server fails the test.
        for target in ("requests.patch", "requests.get", "requests.post", "requests.put", "requests.request"):
            patcher = patch(target, side_effect=AssertionError("PBIRS must not be contacted"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def rename(self, name, report_id="r-1"):
        """POST the rename; returns (response, the flash messages). No redirect is followed: that page calls PBIRS."""
        response = self.client.post(reverse("powerbi_report:edit_report", args=[report_id]), {"name": name})
        return response, [str(m) for m in get_messages(response.wsgi_request)]

    def test_the_name_changes_in_the_app_and_is_kept_by_the_sync(self):
        response, flashes = self.rename("Nouveau nom")
        self.assertRedirects(response, reverse("powerbi_report:report_detail", args=["r-1"]), fetch_redirect_response=False)
        self.ref.refresh_from_db()
        self.assertEqual(self.ref.name, "Nouveau nom")
        self.assertTrue(self.ref.name_locked)
        self.assertEqual(self.ref.path, "/BI/Ancien nom")                       # nothing else moves
        self.assertEqual(self.ref.modified_by, self.admin)
        self.assertIsNotNone(self.ref.modified_at)
        self.assertTrue(any("n'est pas modifié" in m for m in flashes))

    def test_it_works_without_a_pbirs_session(self):
        # No PBIRS password in the session: renaming no longer needs one.
        self.assertNotIn("ldap_password", self.client.session)
        self.rename("Sans session")
        self.ref.refresh_from_db()
        self.assertEqual(self.ref.name, "Sans session")

    def test_the_report_list_shows_the_new_name(self):
        self.rename("Nom affiché")
        html = self.client.get(reverse("powerbi_report:report_list")).content.decode()
        self.assertIn("Nom affiché", html)

    def test_extra_spaces_are_cleaned(self):
        self.rename("   Chiffre   d'affaires \n")
        self.ref.refresh_from_db()
        self.assertEqual(self.ref.name, "Chiffre d'affaires")

    def test_admins_are_notified(self):
        self.rename("Nouveau nom")
        notes = Notification.objects.filter(message__contains="Nouveau nom")
        self.assertEqual({n.user for n in notes}, {self.admin, self.other_admin})
        self.assertTrue(all("Ancien nom" in n.message and "dans CBI" in n.message for n in notes))

    def test_bad_names_change_nothing(self):
        for label, name in (("empty", ""), ("blank", "   "), ("too long", "x" * 256)):
            with self.subTest(label):
                self.rename(name)
                self.ref.refresh_from_db()
                self.assertEqual(self.ref.name, "Ancien nom")
                self.assertFalse(self.ref.name_locked)

    def test_the_same_name_again_is_a_no_op_but_locks_a_name_that_was_not_locked(self):
        self.rename("Ancien nom")                                                # same as PBIRS: it just becomes the app's choice
        self.ref.refresh_from_db()
        self.assertTrue(self.ref.name_locked)
        before = self.ref.modified_at
        _, flashes = self.rename("Ancien nom")
        self.ref.refresh_from_db()
        self.assertEqual(self.ref.modified_at, before)
        self.assertTrue(any("inchangé" in m for m in flashes))

    def test_unknown_report_get_and_non_admin(self):
        _, flashes = self.rename("X", report_id="nope")
        self.assertTrue(any("introuvable" in m for m in flashes))
        self.assertEqual(self.client.get(self.url).status_code, 302)             # GET only redirects
        self.ref.refresh_from_db()
        self.assertEqual(self.ref.name, "Ancien nom")
        reader = User.objects.create_user(username="reader", password="x", ad2000="reader1", status="Active")
        self.client.force_login(reader)
        self.assertEqual(self.client.post(self.url, {"name": "Piraté"}).status_code, 403)
        self.ref.refresh_from_db()
        self.assertEqual(self.ref.name, "Ancien nom")
