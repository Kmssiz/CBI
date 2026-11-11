import tempfile
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from tickets.models import Ticket
from users.models import CustomUser
from users.session_secrets import SESSION_KEY, get_ldap_password, store_ldap_password


class SessionPasswordTests(TestCase):
    def test_password_is_encrypted_at_rest_and_readable(self):
        self.client.force_login(CustomUser.objects.create_user(username='sess', password='x'))
        request = type('R', (), {'session': self.client.session})()
        store_ldap_password(request, 'S3cret!')
        stored = request.session[SESSION_KEY]
        self.assertNotIn('S3cret!', stored)
        self.assertEqual(get_ldap_password(request), 'S3cret!')

    def test_legacy_plaintext_session_value_is_upgraded(self):
        request = type('R', (), {'session': {'ldap_password': 'old-plain'}})()
        self.assertEqual(get_ldap_password(request), 'old-plain')
        self.assertNotIn('ldap_password', request.session)
        self.assertEqual(get_ldap_password(request), 'old-plain')

    def test_garbage_value_reads_as_no_password(self):
        request = type('R', (), {'session': {SESSION_KEY: 'not-a-token'}})()
        self.assertIsNone(get_ldap_password(request))


class ProtectedMediaTests(TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        override = override_settings(MEDIA_ROOT=self.root)
        override.enable()
        self.addCleanup(override.disable)
        self.owner = CustomUser.objects.create_user(username='owner', password='x')
        self.other = CustomUser.objects.create_user(username='other', password='x')
        self.admin = CustomUser.objects.create_superuser(username='boss', password='x')
        png = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
               b'\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82')
        self.ticket = Ticket.objects.create(
            title='t', description='d', ticket_type='bug', created_by=self.owner,
            attachment=SimpleUploadedFile('shot.png', png, content_type='image/png'),
        )
        self.url = '/media/' + self.ticket.attachment.name

    def test_anonymous_is_sent_to_login(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_ticket_attachment_visible_to_creator_and_admin_only(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_path_traversal_is_refused(self):
        self.client.force_login(self.admin)
        Path(self.root, 'secret.txt').write_text('x')
        self.assertIn(self.client.get('/media/../secret.txt').status_code, (400, 404))


class PbirsClientAuthTests(TestCase):
    def test_client_builds_ntlm_auth_from_the_encrypted_session_password(self):
        from django.test import RequestFactory
        from powerbi_report.services.pbirs_client import PBIRSClient
        user = CustomUser.objects.create_user(username='ntlm_user', password='x')
        client = self.client
        client.force_login(user)
        request = RequestFactory().get('/')
        request.user = user
        request.session = client.session
        self.assertIsNone(PBIRSClient(request).auth)  # no password stored yet
        store_ldap_password(request, 'dir-password')
        auth = PBIRSClient(request).auth
        self.assertIsNotNone(auth)
        self.assertEqual(auth.password, 'dir-password')
