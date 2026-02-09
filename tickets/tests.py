from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from .models import Ticket

User = get_user_model()

class TicketTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username='testuser', password='password')
        self.admin = User.objects.create_superuser(username='admin', password='password')
        self.ticket = Ticket.objects.create(
            title='Test Ticket',
            description='Test Description',
            ticket_type='bug',
            category='cbi',
            priority='medium',
            status='open',
            created_by=self.user
        )

    def test_ticket_list_view(self):
        self.client.login(username='testuser', password='password')
        response = self.client.get(reverse('tickets:ticket_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Test Ticket')
        self.assertContains(response, 'CBI')

    def test_create_ticket_view(self):
        self.client.login(username='testuser', password='password')
        response = self.client.post(reverse('tickets:create_ticket'), {
            'title': 'New Ticket',
            'description': 'New Description',
            'ticket_type': 'refresh',
            'category': 'bibliotheque',
            'priority': 'high'
        })
        self.assertEqual(response.status_code, 302) # Redirects after success
        self.assertEqual(Ticket.objects.count(), 2)
        new_ticket = Ticket.objects.get(title='New Ticket')
        self.assertEqual(new_ticket.ticket_type, 'refresh')
        self.assertEqual(new_ticket.category, 'bibliotheque')

    def test_ticket_detail_view(self):
        self.client.login(username='testuser', password='password')
        response = self.client.get(reverse('tickets:ticket_detail', args=[self.ticket.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Test Ticket')

    def test_ticket_access_control(self):
        other_user = User.objects.create_user(username='other', password='password')
        self.client.login(username='other', password='password')
        response = self.client.get(reverse('tickets:ticket_detail', args=[self.ticket.id]))
        self.assertEqual(response.status_code, 302) # Redirects because no permission

    def test_assign_ticket(self):
        self.client.login(username='admin', password='password')
        response = self.client.post(reverse('tickets:assign_ticket', args=[self.ticket.id]), {
            'assigned_to': self.admin.id
        })
        self.assertEqual(response.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.assigned_to, self.admin)
        
    def test_unassign_ticket(self):
        self.ticket.assigned_to = self.admin
        self.ticket.save()
        self.client.login(username='admin', password='password')
        response = self.client.post(reverse('tickets:assign_ticket', args=[self.ticket.id]), {
            'assigned_to': ''
        })
        self.assertEqual(response.status_code, 302)
        self.ticket.refresh_from_db()
        self.assertIsNone(self.ticket.assigned_to)
