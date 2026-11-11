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
        self.assertContains(response, "Mettre a jour")

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

    def test_creating_a_ticket_confirms_to_the_requester_and_alerts_every_admin(self):
        self.client.login(username="requester", password="password")
        self.client.post(reverse("tickets:create_ticket"), {
            "title": "Nouveau", "description": "Desc", "ticket_type": "refresh", "category": "cbi", "priority": "medium",
        })
        self.assertEqual(self._to(), ["admin1@example.com", "admin2@example.com", "requester@example.com"])
        self.assertTrue(all("Nouveau" in m.subject for m in self.mail.outbox))
        self.assertTrue(all(len(m.to) == 1 for m in self.mail.outbox))  # nobody sees anybody's address

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
