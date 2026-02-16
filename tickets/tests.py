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
