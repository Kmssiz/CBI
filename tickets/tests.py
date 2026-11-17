from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from .models import Ticket, TicketMessage

User = get_user_model()


class TicketTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username="testuser", password="password")
        self.admin = User.objects.create_superuser(username="admin", password="password")
        self.ticket = Ticket.objects.create(
            title="Test Ticket",
            description="Test Description",
            ticket_type="bug",
            category="cbi",
            priority="medium",
            status="open",
            created_by=self.user,
        )

    def test_ticket_list_view(self):
        self.client.login(username="testuser", password="password")
        response = self.client.get(reverse("tickets:ticket_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Ticket")
        self.assertContains(response, "CBI")

    def test_create_ticket_view(self):
        self.client.login(username="testuser", password="password")
        response = self.client.post(
            reverse("tickets:create_ticket"),
            {
                "title": "New Ticket",
                "description": "New Description",
                "ticket_type": "refresh",
                "category": "bibliotheque",
                "priority": "high",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Ticket.objects.count(), 2)
        new_ticket = Ticket.objects.get(title="New Ticket")
        self.assertEqual(new_ticket.ticket_type, "refresh")
        self.assertEqual(new_ticket.category, "bibliotheque")

    def test_ticket_detail_view(self):
        self.client.login(username="testuser", password="password")
        response = self.client.get(reverse("tickets:ticket_detail", args=[self.ticket.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test Ticket")

    def test_ticket_access_control(self):
        other_user = User.objects.create_user(username="other", password="password")
        self.client.login(username="other", password="password")
        response = self.client.get(reverse("tickets:ticket_detail", args=[self.ticket.id]))
        self.assertEqual(response.status_code, 302)

    def test_assign_ticket(self):
        self.client.login(username="admin", password="password")
        response = self.client.post(
            reverse("tickets:assign_ticket", args=[self.ticket.id]),
            {"assigned_to": self.admin.id},
        )
        self.assertEqual(response.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.assigned_to, self.admin)

    def test_unassign_ticket(self):
        self.ticket.assigned_to = self.admin
        self.ticket.save()
        self.client.login(username="admin", password="password")
        response = self.client.post(
            reverse("tickets:assign_ticket", args=[self.ticket.id]),
            {"assigned_to": ""},
        )
        self.assertEqual(response.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertIsNone(self.ticket.assigned_to)

    def test_add_message_by_creator(self):
        self.client.login(username="testuser", password="password")
        response = self.client.post(
            reverse("tickets:add_message", args=[self.ticket.id]),
            {"content": "Message du createur"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(TicketMessage.objects.count(), 1)
        message = TicketMessage.objects.get()
        self.assertEqual(message.sender, self.user)
        self.assertEqual(message.ticket, self.ticket)

    def test_add_message_by_admin(self):
        self.client.login(username="admin", password="password")
        response = self.client.post(
            reverse("tickets:add_message", args=[self.ticket.id]),
            {"content": "Message admin"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(TicketMessage.objects.count(), 1)
        self.assertEqual(TicketMessage.objects.get().sender, self.admin)

    def test_add_message_access_denied_for_other_user(self):
        other_user = User.objects.create_user(username="other", password="password")
        self.client.login(username="other", password="password")
        response = self.client.post(
            reverse("tickets:add_message", args=[self.ticket.id]),
            {"content": "Tentative non autorisee"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(TicketMessage.objects.count(), 0)

    def test_messages_are_displayed_in_chronological_order(self):
        TicketMessage.objects.create(
            ticket=self.ticket,
            sender=self.user,
            content="Premier message",
        )
        TicketMessage.objects.create(
            ticket=self.ticket,
            sender=self.admin,
            content="Deuxieme message",
        )

        self.client.login(username="testuser", password="password")
        response = self.client.get(reverse("tickets:ticket_detail", args=[self.ticket.id]))
        self.assertEqual(response.status_code, 200)

        body = response.content.decode("utf-8")
        self.assertLess(body.index("Premier message"), body.index("Deuxieme message"))

    def test_ticket_detail_uses_explicit_submit_buttons(self):
        self.client.login(username="admin", password="password")
        response = self.client.get(reverse("tickets:ticket_detail", args=[self.ticket.id]))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'onchange="this.form.submit()"')
        self.assertContains(response, "Mettre à jour")

    def test_update_ticket_info_updates_status_and_assignment(self):
        self.client.login(username="admin", password="password")
        response = self.client.post(
            reverse("tickets:update_ticket_info", args=[self.ticket.id]),
            {
                "status": "in_progress",
                "assigned_to": str(self.admin.id),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, "in_progress")
        self.assertEqual(self.ticket.assigned_to, self.admin)


class TicketEmailTests(TestCase):
    """Both sides of a ticket hear about it: the requester and the people handling it."""

    def setUp(self):
        from django.core import mail
        self.mail = mail
        self.client = Client()
        self.user = User.objects.create_user(username="requester", password="password", email="requester@example.com")
        self.admin = User.objects.create_superuser(username="admin1", password="password", email="admin1@example.com")
        self.admin2 = User.objects.create_superuser(username="admin2", password="password", email="admin2@example.com")
        self.ticket = Ticket.objects.create(
            title="Dashboard ventes", description="Il manque une colonne.", ticket_type="bug",
            category="cbi", priority="high", status="open", created_by=self.user,
        )

    def _to(self):
        return sorted(addr for m in self.mail.outbox for addr in m.to)

    def test_creating_a_ticket_alerts_every_admin_and_not_the_requester(self):
        self.client.login(username="requester", password="password")
        self.client.post(reverse("tickets:create_ticket"), {
            "title": "Nouveau", "description": "Desc", "ticket_type": "refresh", "category": "cbi", "priority": "medium",
        })
        self.assertEqual(self._to(), ["admin1@example.com", "admin2@example.com"])   # no copy for the sender
        self.assertTrue(all("Nouveau" in m.subject for m in self.mail.outbox))
        self.assertTrue(all(len(m.to) == 1 for m in self.mail.outbox))  # nobody sees anybody's address

    def test_an_admin_opening_a_ticket_is_not_mailed_about_it_but_the_other_admins_are(self):
        self.client.login(username="admin1", password="password")
        self.client.post(reverse("tickets:create_ticket"), {
            "title": "Par un admin", "description": "Desc", "ticket_type": "bug", "category": "cbi", "priority": "low",
        })
        self.assertEqual(self._to(), ["admin2@example.com"])

    def test_creating_a_ticket_notifies_every_admin_in_the_app_with_a_link(self):
        from notifications.models import Notification
        self.client.login(username="requester", password="password")
        self.client.post(reverse("tickets:create_ticket"), {
            "title": "Nouveau", "description": "Desc", "ticket_type": "refresh", "category": "cbi", "priority": "medium",
        })
        ticket = Ticket.objects.get(title="Nouveau")
        link = reverse("tickets:ticket_detail", args=[ticket.id])
        notified = set(Notification.objects.filter(link=link).values_list("user__username", flat=True))
        self.assertEqual(notified, {"admin1", "admin2"})           # the requester is not notified
        note = Notification.objects.filter(link=link).first()
        self.assertIn("Nouveau", note.message)
        self.assertIn(f"#{ticket.id}", note.message)

    def test_a_message_from_the_requester_goes_to_the_handlers_not_back_to_the_sender(self):
        self.client.login(username="requester", password="password")
        self.client.post(reverse("tickets:add_message", args=[self.ticket.id]), {"content": "Des nouvelles ?"})
        self.assertEqual(self._to(), ["admin1@example.com", "admin2@example.com"])
        self.assertIn("Des nouvelles ?", self.mail.outbox[0].body)

    def test_once_assigned_only_the_assigned_admin_is_mailed(self):
        self.ticket.assigned_to = self.admin
        self.ticket.save()
        self.client.login(username="requester", password="password")
        self.client.post(reverse("tickets:add_message", args=[self.ticket.id]), {"content": "Bonjour"})
        self.assertEqual(self._to(), ["admin1@example.com"])

    def test_a_reply_from_an_admin_goes_to_the_requester_only(self):
        self.client.login(username="admin1", password="password")
        self.client.post(reverse("tickets:add_message", args=[self.ticket.id]), {"content": "C'est corrigé."})
        self.assertEqual(self._to(), ["requester@example.com"])
        self.assertIn("C'est corrigé.", self.mail.outbox[0].body)

    def test_changing_the_status_tells_the_requester_and_a_no_op_tells_nobody(self):
        self.client.login(username="admin1", password="password")
        self.client.post(reverse("tickets:change_status", args=[self.ticket.id]), {"status": "in_progress"})
        self.assertEqual(self._to(), ["requester@example.com"])
        self.assertIn("En cours", self.mail.outbox[0].subject)
        self.mail.outbox.clear()
        self.client.post(reverse("tickets:change_status", args=[self.ticket.id]), {"status": "in_progress"})
        self.assertEqual(self.mail.outbox, [])

    def test_assigning_tells_the_new_assignee_but_not_the_admin_who_did_it(self):
        self.client.login(username="admin1", password="password")
        self.client.post(reverse("tickets:assign_ticket", args=[self.ticket.id]), {"assigned_to": self.admin2.id})
        self.assertEqual(self._to(), ["admin2@example.com"])
        self.mail.outbox.clear()
        self.client.post(reverse("tickets:assign_ticket", args=[self.ticket.id]), {"assigned_to": self.admin.id})
        self.assertEqual(self.mail.outbox, [])  # admin1 assigned it to themself

    def test_update_info_sends_status_and_assignment_mails(self):
        self.client.login(username="admin1", password="password")
        self.client.post(reverse("tickets:update_ticket_info", args=[self.ticket.id]),
                         {"status": "closed", "assigned_to": self.admin2.id})
        self.assertEqual(self._to(), ["admin2@example.com", "requester@example.com"])

    def test_people_without_an_address_are_skipped(self):
        User.objects.filter(pk=self.admin2.pk).update(email="")
        self.client.login(username="requester", password="password")
        self.client.post(reverse("tickets:add_message", args=[self.ticket.id]), {"content": "Salut"})
        self.assertEqual(self._to(), ["admin1@example.com"])

    def test_a_mail_failure_never_breaks_the_ticket_action(self):
        from unittest.mock import patch
        self.client.login(username="requester", password="password")
        with patch("django.core.mail.message.EmailMessage.send", side_effect=OSError("smtp down")):
            response = self.client.post(reverse("tickets:add_message", args=[self.ticket.id]), {"content": "Toujours là"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(TicketMessage.objects.filter(ticket=self.ticket, content="Toujours là").exists())

    def test_the_link_in_the_mail_points_to_the_ticket(self):
        from django.test import override_settings
        self.client.login(username="admin1", password="password")
        with override_settings(SITE_URL="https://cbi.example.com"):
            self.client.post(reverse("tickets:change_status", args=[self.ticket.id]), {"status": "closed"})
        self.assertIn(f"https://cbi.example.com/tickets/{self.ticket.id}/", self.mail.outbox[0].body)

    def test_update_mails_the_assigned_admin_even_when_they_pressed_the_button(self):
        self.client.login(username="admin1", password="password")
        self.client.post(reverse("tickets:update_ticket_info", args=[self.ticket.id]),
                         {"status": "open", "assigned_to": self.admin.id})
        self.assertEqual(self._to(), ["admin1@example.com"])
        self.assertIn("assigné", self.mail.outbox[0].subject)

    def test_status_change_on_an_assigned_ticket_mails_the_assigned_admin_and_the_requester(self):
        self.ticket.assigned_to = self.admin2
        self.ticket.save()
        self.client.login(username="admin1", password="password")
        self.client.post(reverse("tickets:update_ticket_info", args=[self.ticket.id]),
                         {"status": "in_progress", "assigned_to": self.admin2.id})
        self.assertEqual(self._to(), ["admin2@example.com", "requester@example.com"])
        self.assertTrue(any("mis à jour" in m.subject for m in self.mail.outbox))

    def test_update_without_any_change_sends_nothing(self):
        self.ticket.assigned_to = self.admin2
        self.ticket.save()
        self.client.login(username="admin1", password="password")
        self.client.post(reverse("tickets:update_ticket_info", args=[self.ticket.id]),
                         {"status": "open", "assigned_to": self.admin2.id})
        self.assertEqual(self.mail.outbox, [])

    def test_mails_have_a_styled_html_version_and_escape_user_text(self):
        from django.test import override_settings
        self.client.login(username="requester", password="password")
        with override_settings(SITE_URL="https://cbi.example.com"):
            self.client.post(reverse("tickets:add_message", args=[self.ticket.id]),
                             {"content": "<script>alert(1)</script>\nDeuxième ligne"})
        message = self.mail.outbox[0]
        html, kind = message.alternatives[0]
        self.assertEqual(kind, "text/html")
        self.assertIn(f"https://cbi.example.com/tickets/{self.ticket.id}/", html)
        self.assertIn("Ouvrir le ticket", html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("<br>", html)  # line breaks of the message are kept
        self.assertIn("Dashboard ventes", html)
        self.assertIn("Deuxième ligne", message.body)  # plain-text version stays complete


class TicketLabelTests(TestCase):
    """The wording asked for: categories 'Bibliothèque' / 'Tableau de bord'; the dashboard request type."""

    def test_categories_are_bibliotheque_and_tableau_de_bord(self):
        self.assertEqual(
            [label for _, label in Ticket.CATEGORY_CHOICES], ["Bibliothèque", "Tableau de bord"]
        )
        self.assertEqual(dict(Ticket.CATEGORY_CHOICES)["cbi"], "Tableau de bord")   # stored value unchanged

    def test_the_dashboard_request_type_is_a_creation_request(self):
        self.assertEqual(dict(Ticket.TICKET_TYPE_CHOICES)["dashboard"], "Demande de création de tableau de bord")

    def test_the_create_form_shows_the_new_wording(self):
        User.objects.create_user(username="someone", password="password")
        self.client.login(username="someone", password="password")
        html = self.client.get(reverse("tickets:create_ticket")).content.decode()
        self.assertIn("Demande de création de tableau de bord", html)
        self.assertIn(">Tableau de bord<", html)
        self.assertNotIn("Demande de dashboard", html)


def _zip_bytes(names):
    import io
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, "<x/>")
    return buffer.getvalue()


PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x1ba\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)
SAMPLES = {
    "rapport.pdf": b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF",
    "note.docx": _zip_bytes(["[Content_Types].xml", "word/document.xml"]),
    "budget.xlsx": _zip_bytes(["[Content_Types].xml", "xl/workbook.xml"]),
    "ancien.doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64,
    "ancien.xls": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64,
    "donnees.csv": "nom;montant\nAlpha;10\nBêta;20\n".encode("utf-8"),
    "capture.png": PNG_1PX,
}


class TicketAttachmentTests(TestCase):
    """Excel, PDF and Word files can be attached, next to images, and anything else is refused."""

    def setUp(self):
        import shutil
        import tempfile
        from django.test import override_settings
        self.media = tempfile.mkdtemp()
        self.override = override_settings(MEDIA_ROOT=self.media)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(shutil.rmtree, self.media, True)
        self.user = User.objects.create_user(username="sender", password="password")
        self.client.login(username="sender", password="password")

    def _create(self, name, content, content_type="application/octet-stream"):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return self.client.post(reverse("tickets:create_ticket"), {
            "title": f"Avec {name}", "description": "Desc", "ticket_type": "bug", "category": "cbi",
            "priority": "medium", "attachment": SimpleUploadedFile(name, content, content_type=content_type),
        })

    def test_each_accepted_format_can_be_attached(self):
        for name, content in SAMPLES.items():
            with self.subTest(name):
                response = self._create(name, content)
                self.assertEqual(response.status_code, 302, response.content.decode()[:300])
                self.assertTrue(Ticket.objects.get(title=f"Avec {name}").attachment)

    def test_refused_files_do_not_create_a_ticket(self):
        refused = {
            "virus.exe": b"MZ\x90\x00",
            "page.html": b"<html><script>alert(1)</script></html>",
            "dessin.svg": b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
            "macro.docm": _zip_bytes(["[Content_Types].xml", "word/document.xml", "word/vbaProject.bin"]),
            "macro.xlsm": _zip_bytes(["[Content_Types].xml", "xl/workbook.xml", "xl/vbaProject.bin"]),
            "faux.pdf": b"this is not a pdf",
            "faux.docx": b"not a zip at all",
            "faux.xlsx": _zip_bytes(["random.txt"]),
            "faux.png": b"GIF89a....",
            "faux.xls": b"plain text",
            "binaire.csv": b"a;b\x00\x01\x02",
        }
        for name, content in refused.items():
            with self.subTest(name):
                response = self._create(name, content)
                self.assertEqual(response.status_code, 200)             # form shown again with an error
                self.assertFalse(Ticket.objects.filter(title=f"Avec {name}").exists())
        self.assertEqual(Ticket.objects.count(), 0)

    def test_a_macro_enabled_document_is_refused_even_when_renamed_docx(self):
        response = self._create("renomme.docx", _zip_bytes(["[Content_Types].xml", "word/document.xml", "word/vbaProject.bin"]))
        self.assertEqual(response.status_code, 200)

    def test_files_over_10_mb_are_refused(self):
        response = self._create("gros.pdf", b"%PDF-1.4" + b"0" * (10 * 1024 * 1024 + 1))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "10 Mo")

    def test_the_form_tells_which_formats_are_accepted(self):
        html = self.client.get(reverse("tickets:create_ticket")).content.decode()
        for fragment in (".pdf", ".docx", ".xlsx", "PDF", "Word", "Excel"):
            self.assertIn(fragment, html)
        self.assertNotIn('accept="image/*"', html)

    def test_a_pdf_shows_as_a_download_chip_and_an_image_as_a_preview(self):
        self._create("rapport.pdf", SAMPLES["rapport.pdf"])
        self._create("capture.png", SAMPLES["capture.png"])
        pdf = Ticket.objects.get(title="Avec rapport.pdf")
        img = Ticket.objects.get(title="Avec capture.png")
        pdf_html = self.client.get(reverse("tickets:ticket_detail", args=[pdf.id])).content.decode()
        self.assertIn("rapport.pdf", pdf_html)
        self.assertIn("picture_as_pdf", pdf_html)
        self.assertNotIn("Pièce jointe du ticket", pdf_html)          # no <img> preview for a PDF
        img_html = self.client.get(reverse("tickets:ticket_detail", args=[img.id])).content.decode()
        self.assertIn("Pièce jointe du ticket", img_html)

    def test_a_reply_can_carry_an_excel_file(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        ticket = Ticket.objects.create(
            title="T", description="D", ticket_type="bug", category="cbi", priority="low", created_by=self.user,
        )
        self.client.post(reverse("tickets:add_message", args=[ticket.id]), {
            "content": "Voici le fichier",
            "attachment": SimpleUploadedFile("budget.xlsx", SAMPLES["budget.xlsx"]),
        })
        message = TicketMessage.objects.get(ticket=ticket)
        self.assertEqual(message.attachment_kind, "excel")
        self.assertFalse(message.attachment_is_image)
        html = self.client.get(reverse("tickets:ticket_detail", args=[ticket.id])).content.decode()
        self.assertIn("budget.xlsx", html)


class TicketAdminAndMailResilienceTests(TestCase):
    """One definition of 'admin' for the whole app, and one refused address never stops the others."""

    def setUp(self):
        from django.core import mail
        self.mail = mail
        self.user = User.objects.create_user(username="req2", password="password", email="req2@example.com")
        self.admin = User.objects.create_superuser(username="adm1", password="password", email="adm1@example.com")
        self.ex_admin = User.objects.create_superuser(username="adm2", password="password", email="adm2@example.com")
        self.ticket = Ticket.objects.create(
            title="Ticket", description="d", ticket_type="bug", category="cbi",
            priority="high", status="open", created_by=self.user,
        )

    def test_a_deactivated_admin_is_not_mailed_and_group_membership_is_not_admin(self):
        from django.contrib.auth.models import Group
        from tickets.views import _get_admin_users, _is_admin
        User.objects.filter(pk=self.ex_admin.pk).update(is_active=False)
        in_group = User.objects.create_user(username="grp", password="password", email="grp@example.com")
        in_group.groups.add(Group.objects.create(name=settings.ADMIN_ROLE_NAME))
        self.assertEqual(set(_get_admin_users().values_list("username", flat=True)), {"adm1"})
        self.assertFalse(_is_admin(in_group))
        self.assertTrue(_is_admin(self.admin))

    def test_one_refused_address_does_not_stop_the_other_mails(self):
        from unittest.mock import patch
        from django.core.mail import EmailMultiAlternatives
        from tickets import emails

        real_send, calls = EmailMultiAlternatives.send, []

        def flaky(message, *args, **kwargs):
            calls.append(message.to[0])
            if message.to[0] == "adm1@example.com":
                raise OSError("recipient refused")
            return real_send(message, *args, **kwargs)

        with patch.object(EmailMultiAlternatives, "send", flaky):
            emails.ticket_created(self.ticket)
        self.assertEqual(sorted(calls), ["adm1@example.com", "adm2@example.com"])
        self.assertEqual([m.to[0] for m in self.mail.outbox], ["adm2@example.com"])

    def test_old_office_files_carrying_macros_are_refused(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.core.exceptions import ValidationError
        from tickets.validators import validate_ticket_attachment
        ole = bytes.fromhex("d0cf11e0a1b11ae1")
        clean = SimpleUploadedFile("ok.doc", ole + bytes(64))
        validate_ticket_attachment(clean)
        macro = SimpleUploadedFile("macro.xls", ole + bytes(32) + "_VBA_PROJECT".encode("utf-16-le") + bytes(8))
        with self.assertRaises(ValidationError):
            validate_ticket_attachment(macro)
