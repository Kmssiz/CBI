import json
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

from mobile.tests import MobileTestCase, png_upload, url
from tickets.models import Ticket, TicketMessage
from users.models import CustomUser


class MobileTicketTests(MobileTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(MEDIA_ROOT=tempfile.mkdtemp()))
        self.admin = CustomUser.objects.create_user(username='admin.bi', first_name='Admin', last_name='BI',
                                                    role=self.admin_role)
        self.admin_auth = self.bearer(self.admin)

    def create(self, auth=None, **fields):
        data = {'title': 'Accès MDM', 'description': 'Merci', 'ticket_type': 'access', **fields}
        return self.client.post(url('tickets'), data=data, **(auth or self.auth))  # multipart

    def test_choices_mirror_the_platform(self):
        body = self.client.get(url('ticket_choices'), **self.auth).json()
        self.assertEqual([c['value'] for c in body['categories']], ['bibliotheque', 'cbi'])
        self.assertIn({'value': 'access', 'label': "Demande d'accès"}, body['ticket_types'])
        self.assertFalse(body['is_admin'])
        self.assertTrue(self.client.get(url('ticket_choices'), **self.admin_auth).json()['is_admin'])

    def test_multipart_creation_with_attachment_uses_the_platform_form(self):
        response = self.create(category='bibliotheque', priority='high', attachment=png_upload('capture.png'))
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual((body['category'], body['priority'], body['status']), ('bibliotheque', 'high', 'open'))
        self.assertEqual(body['created_by']['name'], 'Prenom Nom')
        self.assertIsNotNone(body['attachment_url'])
        image = self.client.get(body['attachment_url'], **self.auth)
        self.assertEqual(image.status_code, 200)
        image.close()

        # Defaults like the web form: JSON body, cbi category, medium priority.
        json_ticket = self.client.post(url('tickets'), data=json.dumps({
            'title': 'Bug', 'description': 'x', 'ticket_type': 'bug'}), content_type='application/json', **self.auth)
        self.assertEqual((json_ticket.json()['category'], json_ticket.json()['priority']), ('cbi', 'medium'))

    def test_form_validation_errors(self):
        too_big = SimpleUploadedFile('big.png', b'0' * (5 * 1024 * 1024 + 1), content_type='image/png')
        self.assertEqual(self.create(attachment=too_big).status_code, 400)
        not_image = SimpleUploadedFile('x.png', b'not an image', content_type='image/png')
        self.assertEqual(self.create(attachment=not_image).status_code, 400)
        missing = self.create(title='')
        self.assertEqual(missing.status_code, 400)
        self.assertIn('title', missing.json()['errors'])
        self.assertEqual(self.create(ticket_type='nope').status_code, 400)

    def test_messages_with_image_and_visibility(self):
        ticket_id = self.create().json()['id']
        reply = self.client.post(url('ticket_messages', ticket_id),
                                 data={'content': 'Voici une capture', 'attachment': png_upload()}, **self.admin_auth)
        self.assertEqual(reply.status_code, 201)
        self.assertTrue(reply.json()['from_admin'])
        detail = self.client.get(url('ticket', ticket_id), **self.auth).json()
        message = detail['messages'][0]
        self.assertFalse(message['is_mine'])
        self.assertFalse(detail['can_manage'])
        attachment = self.client.get(message['attachment_url'], **self.auth)
        self.assertEqual(attachment.status_code, 200)
        attachment.close()

        stranger = CustomUser.objects.create_user(username='stranger', role=self.user_role)
        stranger_auth = self.bearer(stranger)
        self.assertEqual(self.client.get(url('ticket', ticket_id), **stranger_auth).status_code, 404)
        self.assertEqual(self.client.get(message['attachment_url'], **stranger_auth).status_code, 404)
        self.assertEqual(self.client.post(url('ticket_messages', ticket_id), data={'content': 'x'},
                                          **stranger_auth).status_code, 404)

    def test_admin_updates_status_and_assignee_like_the_web(self):
        ticket_id = self.create().json()['id']
        update = url('ticket_update', ticket_id)
        self.assertEqual(self.client.post(update, data=json.dumps({'status': 'closed'}),
                                          content_type='application/json', **self.auth).status_code, 403)
        body = self.client.post(update, data=json.dumps({'status': 'in_progress', 'assigned_to': self.admin.pk}),
                                content_type='application/json', **self.admin_auth).json()
        self.assertEqual((body['status'], body['assigned_to']['id']), ('in_progress', self.admin.pk))
        # Only admins can be assignees.
        bad = self.client.post(update, data=json.dumps({'assigned_to': self.user.pk}),
                               content_type='application/json', **self.admin_auth)
        self.assertEqual(bad.status_code, 400)
        cleared = self.client.post(update, data=json.dumps({'assigned_to': None}),
                                   content_type='application/json', **self.admin_auth).json()
        self.assertIsNone(cleared['assigned_to'])
        self.assertEqual(self.client.post(update, data=json.dumps({'status': 'bogus'}),
                                          content_type='application/json', **self.admin_auth).status_code, 400)

    def test_the_mobile_app_sends_the_same_emails_as_the_web(self):
        from django.core import mail
        CustomUser.objects.filter(pk=self.user.pk).update(email='requester@example.com')
        CustomUser.objects.filter(pk=self.admin.pk).update(email='admin@example.com')
        ticket_id = self.create().json()['id']
        self.assertEqual(sorted(addr for m in mail.outbox for addr in m.to), ['admin@example.com'])   # no copy for the sender
        mail.outbox.clear()
        self.client.post(url('ticket_messages', ticket_id), data={'content': 'Bonjour'}, **self.auth)
        self.assertEqual([m.to for m in mail.outbox], [['admin@example.com']])        # requester -> handlers
        mail.outbox.clear()
        self.client.post(url('ticket_messages', ticket_id), data={'content': 'Réponse'}, **self.admin_auth)
        self.assertEqual([m.to for m in mail.outbox], [['requester@example.com']])    # admin -> requester
        mail.outbox.clear()
        self.client.post(url('ticket_update', ticket_id), data=json.dumps({'status': 'closed'}),
                         content_type='application/json', **self.admin_auth)
        self.assertEqual([m.to for m in mail.outbox], [['requester@example.com']])

    def test_lists_and_admins(self):
        mine = self.create().json()['id']
        other = Ticket.objects.create(title='Autre', description='x', ticket_type='bug', created_by=self.admin,
                                      status='closed')
        self.assertEqual([t['id'] for t in self.client.get(url('tickets'), **self.auth).json()['tickets']], [mine])
        everything = self.client.get(url('tickets'), **self.admin_auth).json()
        self.assertTrue(everything['is_admin'])
        self.assertEqual({t['id'] for t in everything['tickets']}, {mine, other.pk})
        closed = self.client.get(url('tickets'), {'status': 'closed'}, **self.admin_auth).json()['tickets']
        self.assertEqual([t['id'] for t in closed], [other.pk])
        admins = self.client.get(url('ticket_admins'), **self.admin_auth).json()['admins']
        self.assertEqual([a['id'] for a in admins], [self.admin.pk])
        self.assertEqual(self.client.get(url('ticket_admins'), **self.auth).status_code, 403)
        self.assertEqual(TicketMessage.objects.count(), 0)


class MobileTicketAttachmentsAndAlertsTests(MobileTestCase):
    """The mobile app follows the web rules: documents are accepted, admins are alerted, nobody mails the sender."""

    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(MEDIA_ROOT=tempfile.mkdtemp()))
        self.admin = CustomUser.objects.create_user(username='admin.bi', first_name='Admin', last_name='BI',
                                                    role=self.admin_role)
        self.admin_auth = self.bearer(self.admin)

    def post(self, upload, **extra):
        data = {'title': 'Avec fichier', 'description': 'Merci', 'ticket_type': 'dashboard', 'attachment': upload}
        data.update(extra)
        return self.client.post(url('tickets'), data=data, **self.auth)

    def test_choices_advertise_the_real_limit_and_formats(self):
        body = self.client.get(url('ticket_choices'), **self.auth).json()
        self.assertEqual(body['max_attachment_bytes'], 10 * 1024 * 1024)
        for ext in ('.pdf', '.docx', '.xlsx', '.png'):
            self.assertIn(ext, body['attachment_extensions'])
        self.assertNotIn('.exe', body['attachment_extensions'])
        self.assertIn('Excel', body['attachment_formats_help'])
        labels = {c['value']: c['label'] for c in body['ticket_types']}
        self.assertEqual(labels['dashboard'], 'Demande de création de tableau de bord')
        self.assertEqual([c['label'] for c in body['categories']], ['Bibliothèque', 'Tableau de bord'])

    def test_a_pdf_and_an_excel_file_are_accepted_and_described(self):
        pdf = self.post(SimpleUploadedFile('rapport.pdf', b'%PDF-1.4\n%%EOF', content_type='application/pdf'))
        self.assertEqual(pdf.status_code, 201, pdf.content)
        body = pdf.json()
        self.assertEqual((body['attachment_name'], body['attachment_kind']), ('rapport.pdf', 'pdf'))
        download = self.client.get(body['attachment_url'], **self.auth)
        self.assertEqual(download.status_code, 200)
        self.assertIn('attachment', download['Content-Disposition'])      # documents are downloaded, not displayed
        self.assertEqual(download['X-Content-Type-Options'], 'nosniff')
        download.close()

        import io
        import zipfile
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('[Content_Types].xml', '<x/>')
            archive.writestr('xl/workbook.xml', '<x/>')
        xlsx = self.post(SimpleUploadedFile('budget.xlsx', buffer.getvalue()))
        self.assertEqual(xlsx.status_code, 201, xlsx.content)
        self.assertEqual(xlsx.json()['attachment_kind'], 'excel')

    def test_an_image_has_the_image_kind_and_is_not_forced_to_download(self):
        body = self.post(png_upload('capture.png')).json()
        self.assertEqual(body['attachment_kind'], 'image')
        response = self.client.get(body['attachment_url'], **self.auth)
        self.assertNotIn('attachment', response.get('Content-Disposition', ''))   # shown inline, not downloaded
        response.close()

    def test_unsafe_or_fake_files_are_refused_with_a_clear_message(self):
        for name, content in (('virus.exe', b'MZ\x90'), ('page.html', b'<html></html>'), ('fake.pdf', b'nope')):
            with self.subTest(name):
                response = self.post(SimpleUploadedFile(name, content))
                self.assertEqual(response.status_code, 400)
                self.assertIn('attachment', response.json()['errors'])
        self.assertEqual(Ticket.objects.count(), 0)

    def test_every_admin_is_notified_in_the_app_and_the_sender_is_not_mailed(self):
        from django.core import mail
        from notifications.models import Notification
        CustomUser.objects.filter(pk=self.user.pk).update(email='requester@example.com')
        CustomUser.objects.filter(pk=self.admin.pk).update(email='admin@example.com')
        ticket_id = self.post(png_upload('c.png')).json()['id']
        self.assertEqual([m.to for m in mail.outbox], [['admin@example.com']])
        notes = Notification.objects.filter(link=f'/tickets/{ticket_id}/')
        self.assertEqual(sorted(notes.values_list('user__username', flat=True)), ['admin.bi'])
        self.assertFalse(notes.filter(user=self.user).exists())
