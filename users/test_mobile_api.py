import json
import ssl
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.core import signing
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from notifications.models import Notification
from powerbi_report.models import ReportRef, UserReportPermission
from users.models import CustomUser, MobileApiSession, MobileFavorite, Role, UserHistory
from users import ldap_utils
from users.checks import check_mobile_login_security


class MobileApiTests(TestCase):
    def setUp(self):
        role = Role.objects.create(name=settings.USER_ROLE_NAME)
        self.user = CustomUser.objects.create_user(
            username='mobile.user',
            password='local-password',
            role=role,
            status='Active',
            can_view_consolide=True,
        )
        self.report = ReportRef.objects.create(
            pbirs_id='mobile-test-report',
            name='Mobile test report',
            path='/Consolidated/Mobile test report',
            server_url='https://reports.example.test',
            report_type='dashboard',
            is_consolide=True,
        )
        UserReportPermission.objects.create(user=self.user, report=self.report)
        self.mobile_session = MobileApiSession.objects.create(
            user=self.user,
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.token = signing.dumps(
            {'user': self.user.pk, 'sid': str(self.mobile_session.pk)},
            salt='cbi-mobile-auth',
        )
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {self.token}'}

    def test_security_check_flags_insecure_ldap_and_process_local_cache(self):
        warning_ids = {
            warning.id for warning in check_mobile_login_security(None)
        }
        self.assertIn('users.W001', warning_ids)
        self.assertIn('users.W002', warning_ids)

        with override_settings(
            LDAP_USE_SSL=True,
            LDAP_ENABLE_PORT_FALLBACK=False,
            CACHES={
                'default': {
                    'BACKEND': 'shared.cache.Backend',
                    'LOCATION': 'shared',
                },
            },
        ):
            self.assertEqual(check_mobile_login_security(None), [])

    @patch('users.mobile_api.connexion_ad2000')
    @override_settings(LDAP_USE_SSL=True, LDAP_ENABLE_PORT_FALLBACK=False)
    def test_ldap_login_issues_revocable_bearer_without_persisting_password(self, ldap):
        ldap.return_value = {
            'username': 'fresh.user', 'first_name': 'Fresh', 'last_name': 'User',
            'email': 'fresh@example.test', 'ad2000': 'FRESH01', 'ad_groups': [],
        }
        response = self.client.post(
            reverse('mobile_login'),
            data=json.dumps({'username': 'fresh.user', 'password': 'ldap-secret'}),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['token'])
        self.assertEqual(response.json()['user']['username'], 'fresh.user')
        self.assertEqual(response.json()['user']['domain'], settings.LDAP_DOMAIN)
        created = CustomUser.objects.get(username='fresh.user')
        self.assertTrue(created.has_usable_password() is False)
        self.assertTrue(MobileApiSession.objects.filter(user=created).exists())
        self.assertNotIn('ldap_password', self.client.session)

    @patch('users.mobile_api.connexion_ad2000')
    @override_settings(LDAP_USE_SSL=False, LDAP_ENABLE_PORT_FALLBACK=True)
    def test_mobile_login_fails_closed_without_ldaps(self, ldap):
        response = self.client.post(
            reverse('mobile_login'),
            data=json.dumps({'username': 'mobile.user', 'password': 'secret'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 503)
        ldap.assert_not_called()

    @patch('users.mobile_api.connexion_ad2000')
    @override_settings(LDAP_USE_SSL=True, LDAP_ENABLE_PORT_FALLBACK=False)
    def test_mobile_login_rejects_malformed_payload_shapes(self, ldap):
        for payload in ([], None, {'username': [], 'password': 'secret'}):
            response = self.client.post(
                reverse('mobile_login'),
                data=json.dumps(payload),
                content_type='application/json',
            )
            self.assertEqual(response.status_code, 400)
        ldap.assert_not_called()

    def test_mobile_login_throttles_repeated_attempts_for_one_identity(self):
        cache.clear()
        with override_settings(
            LDAP_USE_SSL=True,
            LDAP_ENABLE_PORT_FALLBACK=False,
            MOBILE_LOGIN_RATE_LIMIT_PER_IP=20,
            MOBILE_LOGIN_RATE_LIMIT_PER_USER=2,
            MOBILE_LOGIN_RATE_LIMIT_WINDOW=600,
        ), patch('users.mobile_api.connexion_ad2000', return_value=None) as ldap, patch(
            'users.mobile_api.authenticate', return_value=None
        ):
            for _ in range(2):
                response = self.client.post(
                    reverse('mobile_login'),
                    data=json.dumps({'username': 'mobile.user', 'password': 'wrong'}),
                    content_type='application/json',
                )
                self.assertEqual(response.status_code, 401)

            blocked = self.client.post(
                reverse('mobile_login'),
                data=json.dumps({'username': 'mobile.user', 'password': 'wrong'}),
                content_type='application/json',
            )

        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked['Retry-After'], '600')
        self.assertEqual(ldap.call_count, 2)

    def test_mobile_login_throttles_many_identities_from_one_ip(self):
        cache.clear()
        with override_settings(
            LDAP_USE_SSL=True,
            LDAP_ENABLE_PORT_FALLBACK=False,
            MOBILE_LOGIN_RATE_LIMIT_PER_IP=2,
            MOBILE_LOGIN_RATE_LIMIT_PER_USER=20,
            MOBILE_LOGIN_RATE_LIMIT_WINDOW=600,
        ), patch('users.mobile_api.connexion_ad2000', return_value=None) as ldap, patch(
            'users.mobile_api.authenticate', return_value=None
        ):
            for username in ('first.user', 'second.user'):
                response = self.client.post(
                    reverse('mobile_login'),
                    data=json.dumps({'username': username, 'password': 'wrong'}),
                    content_type='application/json',
                )
                self.assertEqual(response.status_code, 401)

            blocked = self.client.post(
                reverse('mobile_login'),
                data=json.dumps({'username': 'third.user', 'password': 'wrong'}),
                content_type='application/json',
            )

        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(ldap.call_count, 2)

    @override_settings(
        LDAP_SERVER_NAME='ldap.example.test',
        LDAP_PORT=636,
        LDAP_USE_SSL=True,
        LDAP_ENABLE_PORT_FALLBACK=False,
        LDAP_CA_CERT_FILE='',
    )
    def test_ldaps_connection_requires_a_trusted_matching_certificate(self):
        with patch('users.ldap_utils.Server') as make_server, patch(
            'users.ldap_utils.Connection'
        ) as make_connection:
            connection = ldap_utils._bind_connection('EXAMPLE\\user', 'secret')

        self.assertIs(connection, make_connection.return_value)
        tls = make_server.call_args.kwargs['tls']
        self.assertEqual(tls.validate, ssl.CERT_REQUIRED)
        self.assertIsNone(tls.ca_certs_file)
        self.assertIsNone(tls.valid_names)  # ldap3 also checks the Server hostname

    def test_bootstrap_and_favorites_use_existing_report_permission(self):
        response = self.client.get(reverse('mobile_bootstrap'), **self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['consolidated'][0]['id'], str(self.report.pk))
        self.assertEqual(response.json()['consolidated'][0]['auth_host'], 'reports.example.test')
        self.assertTrue(response.json()['consolidated'][0]['auth_https'])

        response = self.client.put(
            reverse('mobile_favorite', args=[self.report.pk]),
            data=json.dumps({'favorite': True}),
            content_type='application/json',
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(MobileFavorite.objects.filter(user=self.user, report=self.report).exists())

        response = self.client.get(reverse('mobile_bootstrap'), **self.auth)
        self.assertEqual(response.json()['favorites'][0]['id'], str(self.report.pk))

    def test_mobile_history_is_private_and_returns_recent_actions(self):
        own = UserHistory.objects.create(user=self.user, action='Rapport consulté : Sales')
        other = CustomUser.objects.create_user(username='other.history', password='pass')
        UserHistory.objects.create(user=other, action='Rapport consulté : Secret')

        response = self.client.get(reverse('mobile_history'), **self.auth)

        self.assertEqual(response.status_code, 200)
        self.assertEqual([record['id'] for record in response.json()['history']], [own.pk])
        self.assertEqual(response.json()['history'][0]['action'], 'Rapport consulté : Sales')

    def test_opening_mobile_report_adds_existing_user_history_entry(self):
        token = signing.dumps(
            {'user': self.user.pk, 'report': self.report.pk, 'sid': str(self.mobile_session.pk)},
            salt='mobile-pbirs',
        )

        response = self.client.get(
            reverse('mobile_embed', args=[self.report.pk]),
            {'token': token},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            UserHistory.objects.filter(user=self.user, action__startswith='Rapport consulté').count(),
            1,
        )

    def test_notifications_include_read_state_and_support_user_scoped_updates(self):
        first = Notification.objects.create(user=self.user, message='First alert')
        Notification.objects.create(user=self.user, message='Second alert')
        other_user = CustomUser.objects.create_user(
            username='other.mobile.user',
            password='local-password',
            role=self.user.role,
            status='Active',
        )
        foreign = Notification.objects.create(user=other_user, message='Private alert')

        bootstrap = self.client.get(reverse('mobile_bootstrap'), **self.auth)
        self.assertEqual(bootstrap.status_code, 200)
        self.assertEqual(bootstrap.json()['unread_notification_count'], 2)
        notifications = {item['id']: item for item in bootstrap.json()['notifications']}
        self.assertFalse(notifications[first.pk]['is_read'])

        marked = self.client.put(
            reverse('mobile_notification_read', args=[first.pk]),
            **self.auth,
        )
        self.assertEqual(marked.status_code, 200)
        self.assertTrue(Notification.objects.get(pk=first.pk).is_read)

        foreign_result = self.client.put(
            reverse('mobile_notification_read', args=[foreign.pk]),
            **self.auth,
        )
        self.assertEqual(foreign_result.status_code, 404)

        marked_all = self.client.put(
            reverse('mobile_notifications_read_all'),
            **self.auth,
        )
        self.assertEqual(marked_all.status_code, 200)
        self.assertEqual(marked_all.json()['updated'], 1)

        bootstrap = self.client.get(reverse('mobile_bootstrap'), **self.auth)
        self.assertEqual(bootstrap.json()['unread_notification_count'], 0)

    def test_favorite_and_embed_reject_reports_without_user_permission(self):
        restricted = ReportRef.objects.create(
            pbirs_id='restricted-mobile-report',
            name='Restricted report',
            path='/Restricted/Report',
            report_type='dashboard',
        )
        favorite = self.client.put(
            reverse('mobile_favorite', args=[restricted.pk]),
            data=json.dumps({'favorite': True}),
            content_type='application/json',
            **self.auth,
        )
        self.assertEqual(favorite.status_code, 404)

        embed_token = signing.dumps(
            {'user': self.user.pk, 'report': restricted.pk, 'sid': str(self.mobile_session.pk)},
            salt='mobile-pbirs',
        )
        embed = self.client.get(
            reverse('mobile_embed', args=[restricted.pk]),
            {'token': embed_token},
        )
        self.assertEqual(embed.status_code, 403)

    def test_logout_revokes_api_and_report_links(self):
        embed_token = signing.dumps(
            {'user': self.user.pk, 'report': self.report.pk, 'sid': str(self.mobile_session.pk)},
            salt='mobile-pbirs',
        )
        logout = self.client.post(reverse('mobile_logout'), **self.auth)
        self.assertEqual(logout.status_code, 200)

        bootstrap = self.client.get(reverse('mobile_bootstrap'), **self.auth)
        self.assertEqual(bootstrap.status_code, 401)
        embed = self.client.get(
            reverse('mobile_embed', args=[self.report.pk]),
            {'token': embed_token},
        )
        self.assertEqual(embed.status_code, 403)

    def test_bearer_mutations_work_with_csrf_middleware_enforced(self):
        client = Client(enforce_csrf_checks=True)
        favorite = client.put(
            reverse('mobile_favorite', args=[self.report.pk]),
            data=json.dumps({'favorite': True}),
            content_type='application/json',
            **self.auth,
        )
        self.assertEqual(favorite.status_code, 200)

        logout = client.post(reverse('mobile_logout'), **self.auth)
        self.assertEqual(logout.status_code, 200)
