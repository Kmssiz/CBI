import json
import ssl
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from notifications.models import Notification
from powerbi_report.models import MetadataOption, PBIRSServer, ReportRef, UserReportPermission
from tickets.models import Ticket
from users import ldap_utils
from users.checks import check_mobile_login_security
from users.models import CustomUser, MobileApiSession, MobileFavorite, Role, UserHistory

from mobile.auth import resolve_directory_username
from mobile.catalog import describe_location, make_code
from mobile.http import issue_token
from mobile.serializers import notification_kind


def url(name, *args):
    return reverse(f'mobile:{name}', args=args)


class MobileTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.user_role = Role.objects.create(name=settings.USER_ROLE_NAME)
        self.admin_role = Role.objects.create(name=settings.ADMIN_ROLE_NAME)
        self.user = CustomUser.objects.create_user(
            username='H0000001', password='local-password', email='prenom.nom@groupe.test',
            first_name='Prenom', last_name='Nom', role=self.user_role, status='Active',
            can_view_consolide=True, can_view_pole=True, direction='DSI', societe='GSH',
        )
        self.server = PBIRSServer.objects.create(name='PBIRS principal', base_url='http://10.20.10.63/')
        self.auth = self.bearer(self.user)

    def bearer(self, user):
        session = MobileApiSession.objects.create(user=user, expires_at=timezone.now() + timedelta(days=1))
        return {'HTTP_AUTHORIZATION': f'Bearer {issue_token(session)}'}

    def option(self, option_type, name, parent=None):
        return MetadataOption.objects.create(option_type=option_type, name=name, parent=parent)

    def report(self, name, grant=True, **fields):
        tags = {key: fields.pop(key) for key in ('poles', 'directions', 'societes', 'modules') if key in fields}
        fields.setdefault('report_type', 'dashboard')
        report = ReportRef.objects.create(
            pbirs_id=f'id-{name}', name=name, path=f'/Tests/{name}',
            server_url='http://10.20.10.63', **fields,
        )
        for key, options in tags.items():
            getattr(report, key).set(options)
        if grant:
            UserReportPermission.objects.create(user=self.user, report=report)
        return report

    def post_json(self, name, *args, data=None, headers=None, client=None):
        return (client or self.client).post(
            url(name, *args), data=json.dumps(data or {}), content_type='application/json',
            **(headers if headers is not None else self.auth),
        )


class HelperTests(TestCase):
    def test_codes_are_initials_without_french_stopwords(self):
        self.assertEqual(make_code('Direction Finance et Comptabilité'), 'DFC')
        self.assertEqual(make_code("Direction Achat et Approvisionnement"), 'DAA')
        self.assertEqual(make_code('MDM'), 'MDM')
        self.assertEqual(make_code('Pôle Production'), 'PP')
        self.assertEqual(make_code(''), '')

    def test_notification_kind_uses_whole_words(self):
        self.assertEqual(notification_kind('Your role has been updated to admin.')[0], 'access')
        self.assertEqual(notification_kind("Accès accordé au rapport X")[0], 'access')
        self.assertEqual(notification_kind("Le rapport 'CA' a été remplacé")[0], 'report')
        self.assertEqual(notification_kind('Tableau de contrôle mis à jour')[0], 'info')

    @override_settings(LDAP_DOMAIN='GSH')
    def test_identifier_resolution_always_yields_an_ntlm_username(self):
        CustomUser.objects.create_user(username='H0000009', email='known.person@groupe.test')
        self.assertEqual(resolve_directory_username('  H0000009 '), 'H0000009')
        self.assertEqual(resolve_directory_username('GSH\\H0000009'), 'H0000009')
        self.assertEqual(resolve_directory_username('KNOWN.person@groupe.test'), 'H0000009')
        self.assertEqual(resolve_directory_username('H0000010@groupe-hasnaoui.local'), 'H0000010')
        self.assertEqual(resolve_directory_username('h0000011@gsh.com'), 'h0000011')
        self.assertIsNone(resolve_directory_username('stranger@elsewhere.com'))


class ConfigAndChecksTests(MobileTestCase):
    @override_settings(MOBILE_MIN_APP_VERSION='3.1.0', MOBILE_CONTACT_EMAIL='bi@example.test')
    def test_config_is_public(self):
        response = self.client.get(url('config'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['min_version'], '3.1.0')
        self.assertEqual(response.json()['contact']['email'], 'bi@example.test')

    def test_login_no_longer_requires_ldaps_but_cache_warning_remains(self):
        ids = {warning.id for warning in check_mobile_login_security(None)}
        self.assertEqual(ids, {'users.W002'})

    @override_settings(
        LDAP_SERVER_NAME='ldap.example.test', LDAP_PORT=636, LDAP_USE_SSL=True,
        LDAP_ENABLE_PORT_FALLBACK=False, LDAP_CA_CERT_FILE='',
    )
    def test_ldaps_connection_requires_a_trusted_certificate(self):
        with patch('users.ldap_utils.Server') as make_server, patch('users.ldap_utils.Connection') as make_connection:
            connection = ldap_utils._bind_connection('EXAMPLE\\user', 'secret')
        self.assertIs(connection, make_connection.return_value)
        self.assertEqual(make_server.call_args.kwargs['tls'].validate, ssl.CERT_REQUIRED)


@override_settings(LDAP_DOMAIN='GSH', LDAP_USE_SSL=False, LDAP_ENABLE_PORT_FALLBACK=True)
class LoginTests(MobileTestCase):
    ldap_info = {'username': 'H0000001', 'first_name': 'Prenom', 'last_name': 'Nom',
                 'email': 'prenom.nom@groupe.test', 'ad2000': 'H0000001'}

    def login(self, username='H0000001', password='ldap-secret', **extra):
        return self.post_json('login', data={'username': username, 'password': password, **extra}, headers={})

    @patch('mobile.auth.connexion_ad2000')
    def test_email_login_binds_with_ntlm_username_and_never_stores_password(self, ldap):
        self.user.ad_groups = ['BI-Readers']
        self.user.save()
        ldap.return_value = self.ldap_info
        response = self.login('Prenom.Nom@groupe.test', device='Pixel 8', app_version='3.0.0')

        self.assertEqual(response.status_code, 200)
        ldap.assert_called_once_with('H0000001', 'ldap-secret')  # no '@' → NTLM bind
        body = response.json()
        self.assertEqual(body['credentials'], {'domain': 'GSH', 'username': 'H0000001'})
        self.assertEqual(body['user']['name'], 'Prenom Nom')
        self.assertNotIn('ldap-secret', response.content.decode())
        session = MobileApiSession.objects.get(user=self.user, device='Pixel 8')
        self.assertEqual(session.app_version, '3.0.0')
        self.user.refresh_from_db()
        self.assertEqual(self.user.ad_groups, ['BI-Readers'])  # sync'd groups are kept

        me = self.client.get(url('me'), HTTP_AUTHORIZATION=f"Bearer {body['token']}")
        self.assertEqual(me.status_code, 200)

    @patch('mobile.auth.connexion_ad2000')
    def test_first_login_creates_directory_user_without_usable_password(self, ldap):
        ldap.return_value = {**self.ldap_info, 'username': 'H0000002', 'email': 'new@groupe.test', 'ad2000': 'H0000002'}
        response = self.login('H0000002')
        self.assertEqual(response.status_code, 200)
        created = CustomUser.objects.get(username='H0000002')
        self.assertFalse(created.has_usable_password())
        self.assertEqual(created.role.name, settings.USER_ROLE_NAME)

    @patch('mobile.auth.connexion_ad2000')
    def test_unknown_email_is_rejected_before_contacting_ldap(self, ldap):
        response = self.login('nobody@elsewhere.com')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['code'], 'identifier_unknown')
        ldap.assert_not_called()

    @patch('mobile.auth.connexion_ad2000', return_value=None)
    def test_local_account_fallback_and_invalid_credentials(self, _ldap):
        self.assertEqual(self.login(password='local-password').status_code, 200)
        wrong = self.login(password='nope')
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(wrong.json()['code'], 'invalid_credentials')

    @patch('mobile.auth.connexion_ad2000', return_value=None)
    def test_inactive_account_is_refused(self, _ldap):
        self.user.is_active = False
        self.user.save()
        # ModelBackend refuses inactive users outright.
        self.assertEqual(self.login(password='local-password').status_code, 401)

    @patch('mobile.auth.connexion_ad2000')
    def test_malformed_payloads(self, ldap):
        for body in ('[]', 'null', '{"username": [], "password": "x"}', 'not json', '{"username": "a"}'):
            response = self.client.post(url('login'), data=body, content_type='application/json')
            self.assertEqual(response.status_code, 400, body)
        ldap.assert_not_called()

    @override_settings(MOBILE_LOGIN_RATE_LIMIT_PER_USER=2, MOBILE_LOGIN_RATE_LIMIT_PER_IP=50, MOBILE_LOGIN_RATE_LIMIT_WINDOW=600)
    @patch('mobile.auth.connexion_ad2000', return_value=None)
    def test_throttles_one_identity(self, ldap):
        for _ in range(2):
            self.assertEqual(self.login(password='wrong').status_code, 401)
        blocked = self.login(password='wrong')
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked['Retry-After'], '600')
        self.assertEqual(ldap.call_count, 2)

    @override_settings(MOBILE_LOGIN_RATE_LIMIT_PER_USER=50, MOBILE_LOGIN_RATE_LIMIT_PER_IP=2)
    @patch('mobile.auth.connexion_ad2000', return_value=None)
    def test_throttles_one_ip(self, _ldap):
        self.assertEqual(self.login('H1').status_code, 401)
        self.assertEqual(self.login('H2').status_code, 401)
        self.assertEqual(self.login('H3').status_code, 429)


class SessionTests(MobileTestCase):
    def test_missing_malformed_and_revoked_tokens(self):
        self.assertEqual(self.client.get(url('me')).json()['code'], 'auth_required')
        self.assertEqual(self.client.get(url('me'), HTTP_AUTHORIZATION='Bearer garbage').status_code, 401)

        logout = self.client.post(url('logout'), **self.auth)
        self.assertEqual(logout.status_code, 200)
        self.assertEqual(self.client.get(url('me'), **self.auth).json()['code'], 'session_expired')

    def test_malformed_session_id_in_a_signed_token(self):
        from django.core import signing
        token = signing.dumps({'user': self.user.pk, 'sid': 'not-a-uuid'}, salt='cbi-mobile-auth-v1')
        self.assertEqual(self.client.get(url('me'), HTTP_AUTHORIZATION=f'Bearer {token}').status_code, 401)

    def test_expired_session_and_inactive_user(self):
        MobileApiSession.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.client.get(url('me'), **self.auth).status_code, 401)
        MobileApiSession.objects.update(expires_at=timezone.now() + timedelta(days=1))
        CustomUser.objects.filter(pk=self.user.pk).update(is_active=False)
        self.assertEqual(self.client.get(url('me'), **self.auth).json()['code'], 'account_inactive')

    @override_settings(MOBILE_SESSION_DAYS=30)
    def test_session_expiry_slides_with_use(self):
        self.client.get(url('me'), **self.auth)
        session = MobileApiSession.objects.get(user=self.user)
        self.assertIsNotNone(session.last_used_at)
        self.assertGreater(session.expires_at, timezone.now() + timedelta(days=29))

    def test_wrong_method_and_csrf_enforcing_client(self):
        self.assertEqual(self.client.delete(url('me'), **self.auth).status_code, 405)
        report = self.report('CSRF', is_consolide=True)
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.put(url('favorite', report.pk), **self.auth).status_code, 200)
        self.assertEqual(client.post(url('logout'), **self.auth).status_code, 200)

    def test_me_describes_the_user(self):
        body = self.client.get(url('me'), **self.auth).json()
        self.assertEqual(body['initials'], 'PN')
        self.assertEqual(body['description'], 'DSI · GSH')
        self.assertFalse(body['is_admin'])
        self.assertIsNone(body['photo_url'])
        self.assertTrue(body['permissions']['consolide'])
        self.assertEqual(self.client.get(url('me_photo'), **self.auth).status_code, 404)


class CatalogTests(MobileTestCase):
    def setUp(self):
        super().setUp()
        self.dfc = self.option('direction', 'Direction Finance et Comptabilité')
        self.dco = self.option('direction', 'Direction Commerce')
        self.production = self.option('pole', 'Pôle Production')
        self.mdm = self.option('societe', 'MDM', parent=self.production)
        self.consolidated = self.report('Encaissement Clients', is_consolide=True, directions=[self.dfc])
        self.untagged = self.report('Activité Consolidé', is_consolide=True)
        self.pole_report = self.report('Production globale', poles=[self.production], directions=[self.dco])
        self.company_report = self.report("Chiffre d'affaire - MDM", poles=[self.production],
                                          societes=[self.mdm], directions=[self.dco])
        self.hidden = self.report('Secret', grant=False, is_consolide=True, directions=[self.dfc])

    def catalog(self, **headers):
        return self.client.get(url('catalog'), **self.auth, **headers)

    def section(self, body, key):
        return next(section for section in body['sections'] if section['key'] == key)

    def test_sections_follow_the_legacy_navigation(self):
        body = self.catalog().json()
        self.assertEqual([s['key'] for s in body['sections']], ['consolide', 'pole', 'societe'])

        consolide = self.section(body, 'consolide')
        self.assertEqual(len(consolide['groups']), 1)
        tabs = consolide['groups'][0]['tabs']
        self.assertEqual([(t['name'], t['code']) for t in tabs],
                         [('Direction Finance et Comptabilité', 'DFC'), ('Général', 'GENERAL')])
        self.assertEqual(tabs[0]['report_ids'], [self.consolidated.pk])  # hidden report excluded

        pole_group = self.section(body, 'pole')['groups'][0]
        self.assertEqual(pole_group['name'], 'Pôle Production')
        self.assertEqual(pole_group['tabs'][0]['report_ids'], [self.pole_report.pk])  # société report moved out

        company = self.section(body, 'societe')
        self.assertEqual(company['layout'], 'grid')
        self.assertEqual(company['groups'][0]['parent'], 'Pôle Production')
        self.assertEqual(company['groups'][0]['tabs'][0]['report_ids'], [self.company_report.pk])

        self.assertNotIn(str(self.hidden.pk), body['reports'])
        report = body['reports'][str(self.consolidated.pk)]
        self.assertEqual(report['location'], 'Consolidé / DFC')
        self.assertEqual(report['embed_url'],
                         'http://10.20.10.63/Reports/powerbi/Tests/Encaissement%20Clients?rs:embed=true')
        self.assertEqual(body['servers'], [{'id': self.server.pk, 'name': 'PBIRS principal',
                                            'base_url': 'http://10.20.10.63', 'host': '10.20.10.63', 'scheme': 'http'}])
        self.assertEqual(report['server_id'], self.server.pk)

    def test_reports_on_additional_servers_are_exposed_for_the_ntlm_allowlist(self):
        other = PBIRSServer.objects.create(name='PBIRS Filiales', base_url='https://bi2.groupe.test')
        ReportRef.objects.filter(pk=self.pole_report.pk).update(server_url='https://bi2.groupe.test/')
        body = self.catalog().json()
        self.assertEqual({s['host'] for s in body['servers']}, {'10.20.10.63', 'bi2.groupe.test'})
        report = body['reports'][str(self.pole_report.pk)]
        self.assertEqual(report['server_id'], other.pk)
        self.assertTrue(report['embed_url'].startswith('https://bi2.groupe.test/Reports/powerbi/'))

    def test_direction_section_only_adds_reports_not_shown_elsewhere(self):
        CustomUser.objects.filter(pk=self.user.pk).update(can_view_direction=True)
        direction_only = self.report('Suivi DCO', directions=[self.dco])
        sections = {s['key']: s for s in self.catalog().json()['sections']}
        ids = [i for g in sections['direction']['groups'] for t in g['tabs'] for i in t['report_ids']]
        self.assertEqual(ids, [direction_only.pk])

        # Without the consolidé view, consolidé reports are reachable through their direction.
        CustomUser.objects.filter(pk=self.user.pk).update(can_view_consolide=False)
        sections = {s['key']: s for s in self.catalog().json()['sections']}
        ids = {i for g in sections['direction']['groups'] for t in g['tabs'] for i in t['report_ids']}
        self.assertIn(self.consolidated.pk, ids)

    def test_view_flags_gate_sections(self):
        CustomUser.objects.filter(pk=self.user.pk).update(can_view_pole=False)
        keys = [s['key'] for s in self.catalog().json()['sections']]
        self.assertEqual(keys, ['consolide'])

    def test_admin_sees_every_report(self):
        admin = CustomUser.objects.create_user(username='admin.bi', role=self.admin_role)
        body = self.client.get(url('catalog'), **self.bearer(admin)).json()
        self.assertIn(str(self.hidden.pk), body['reports'])

    def test_etag_and_favorites(self):
        first = self.catalog()
        self.assertEqual(self.catalog(HTTP_IF_NONE_MATCH=first['ETag']).status_code, 304)
        self.client.put(url('favorite', self.consolidated.pk), **self.auth)
        changed = self.catalog(HTTP_IF_NONE_MATCH=first['ETag'])
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()['favorite_ids'], [self.consolidated.pk])
        self.assertTrue(changed.json()['reports'][str(self.consolidated.pk)]['favorite'])

    def test_user_without_permissions_gets_no_sections(self):
        stranger = CustomUser.objects.create_user(username='nobody', role=self.user_role, can_view_consolide=True)
        body = self.client.get(url('catalog'), **self.bearer(stranger)).json()
        self.assertEqual(body['sections'], [])
        self.assertEqual(body['reports'], {})

    def test_describe_location(self):
        self.assertEqual(describe_location(self.company_report), 'MDM / DC')
        self.assertEqual(describe_location(self.untagged), 'Consolidé')


class ReportAndFavoriteTests(MobileTestCase):
    def setUp(self):
        super().setUp()
        self.allowed = self.report('Allowed', is_consolide=True)
        self.forbidden = self.report('Forbidden', grant=False, is_consolide=True)

    def test_open_and_close_record_the_consultation(self):
        opened = self.post_json('report_open', self.allowed.pk)
        self.assertEqual(opened.status_code, 200)
        body = opened.json()
        self.assertEqual(body['server']['host'], '10.20.10.63')
        self.assertIn('/Reports/powerbi/Tests/Allowed?rs:embed=true', body['embed_url'])
        entry = UserHistory.objects.get(pk=body['view_id'])
        self.assertEqual((entry.report, entry.source), (self.allowed, 'mobile'))
        self.assertTrue(entry.action.startswith('Rapport consulté'))

        closed = self.post_json('report_close', self.allowed.pk, data={'view_id': entry.pk, 'duration_seconds': 184})
        self.assertEqual(closed.status_code, 200)
        entry.refresh_from_db()
        self.assertEqual(entry.duration_seconds, 184)

        other = CustomUser.objects.create_user(username='other', role=self.user_role)
        foreign = self.post_json('report_close', self.allowed.pk, data={'view_id': entry.pk, 'duration_seconds': 1},
                                 headers=self.bearer(other))
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(self.post_json('report_close', self.allowed.pk, data={'view_id': 'x'}).status_code, 400)

    def test_forbidden_reports_are_invisible(self):
        self.assertEqual(self.post_json('report_open', self.forbidden.pk).status_code, 404)
        self.assertEqual(self.client.get(url('report', self.forbidden.pk), **self.auth).status_code, 404)
        self.assertEqual(self.client.put(url('favorite', self.forbidden.pk), **self.auth).status_code, 404)
        self.assertEqual(self.client.get(url('report', self.allowed.pk), **self.auth).json()['name'], 'Allowed')

    def test_favorites_roundtrip(self):
        self.assertTrue(self.client.put(url('favorite', self.allowed.pk), **self.auth).json()['favorite'])
        self.client.put(url('favorite', self.allowed.pk), **self.auth)  # idempotent
        self.assertEqual(MobileFavorite.objects.filter(user=self.user).count(), 1)
        listed = self.client.get(url('favorites'), **self.auth).json()['favorites']
        self.assertEqual([item['id'] for item in listed], [self.allowed.pk])

        # A favourite whose permission was revoked disappears from the list.
        UserReportPermission.objects.filter(report=self.allowed).delete()
        self.assertEqual(self.client.get(url('favorites'), **self.auth).json()['favorites'], [])

        self.assertFalse(self.client.delete(url('favorite', self.allowed.pk), **self.auth).json()['favorite'])
        self.assertFalse(MobileFavorite.objects.exists())


class NotificationTests(MobileTestCase):
    def setUp(self):
        super().setUp()
        self.old = Notification.objects.create(user=self.user, message='Your role has been updated to user.')
        Notification.objects.filter(pk=self.old.pk).update(created_at=timezone.now() - timedelta(days=10))
        self.recent = Notification.objects.create(user=self.user, message="Le rapport 'CA' a été remplacé.")
        other = CustomUser.objects.create_user(username='other', role=self.user_role)
        self.foreign = Notification.objects.create(user=other, message='Private')

    def test_list_poll_and_counts(self):
        body = self.client.get(url('notifications'), **self.auth).json()
        self.assertEqual([n['id'] for n in body['notifications']], [self.recent.pk, self.old.pk])
        self.assertEqual(body['unread_count'], 2)
        self.assertEqual(body['latest_id'], self.recent.pk)
        recent, old = body['notifications']
        self.assertEqual((recent['kind'], recent['title'], recent['is_new']), ('report', 'Rapport', True))
        self.assertEqual((old['kind'], old['is_new']), ('access', False))

        polled = self.client.get(url('notifications'), {'after': self.old.pk}, **self.auth).json()
        self.assertEqual([n['id'] for n in polled['notifications']], [self.recent.pk])
        count = self.client.get(url('notifications_unread_count'), **self.auth).json()
        self.assertEqual(count, {'unread_count': 2, 'latest_id': self.recent.pk})

    def test_read_delete_and_scoping(self):
        self.assertEqual(self.post_json('notification_read', self.recent.pk).json()['unread_count'], 1)
        self.assertEqual(self.post_json('notification_read', self.foreign.pk).status_code, 404)
        self.assertEqual(self.client.delete(url('notification', self.foreign.pk), **self.auth).status_code, 404)
        self.assertEqual(self.client.delete(url('notification', self.recent.pk), **self.auth).status_code, 200)
        self.assertEqual(self.post_json('notifications_read_all').json()['unread_count'], 0)
        self.assertFalse(Notification.objects.get(pk=self.foreign.pk).is_read)


class HistoryTests(MobileTestCase):
    def setUp(self):
        super().setUp()
        self.report_ref = self.report('Ventes', is_consolide=True)
        self.mine = UserHistory.objects.create(user=self.user, action='Rapport consulté : Ventes (mobile)',
                                               report=self.report_ref, duration_seconds=60, source='mobile')
        UserHistory.objects.create(user=self.user, action='Rapport consulté : Achats')  # web entry
        UserHistory.objects.create(user=self.user, action='Utilisateur connecté (mobile)')
        self.other = CustomUser.objects.create_user(username='other', first_name='Autre', role=self.user_role,
                                                    societe='MDM')
        UserHistory.objects.create(user=self.other, action='Rapport consulté : Secret')
        self.admin = CustomUser.objects.create_user(username='admin.bi', role=self.admin_role)

    def test_own_history_lists_only_consultations(self):
        history = self.client.get(url('history'), **self.auth).json()['history']
        self.assertEqual({h['report_name'] for h in history}, {'Ventes', 'Achats'})
        mobile_entry = next(h for h in history if h['id'] == self.mine.pk)
        self.assertEqual((mobile_entry['duration_seconds'], mobile_entry['source']), (60, 'mobile'))

    def test_user_overview_is_admin_only(self):
        self.assertEqual(self.client.get(url('history_users'), **self.auth).status_code, 403)
        self.assertEqual(self.client.get(url('history_user', self.other.pk), **self.auth).status_code, 403)
        self.assertEqual(self.client.get(url('history_user', self.user.pk), **self.auth).status_code, 200)
        self.assertEqual(self.client.get(url('user_photo', self.other.pk), **self.auth).status_code, 403)

        admin_auth = self.bearer(self.admin)
        body = self.client.get(url('history_users'), **admin_auth).json()
        self.assertEqual(body['count'], 2)
        self.assertTrue(all(row['last'] for row in body['results']))
        filtered = self.client.get(url('history_users'), {'company': 'mdm'}, **admin_auth).json()
        self.assertEqual([row['user']['id'] for row in filtered['results']], [self.other.pk])
        by_name = self.client.get(url('history_users'), {'q': 'autre'}, **admin_auth).json()
        self.assertEqual(by_name['count'], 1)
        detail = self.client.get(url('history_user', self.other.pk), **admin_auth).json()
        self.assertEqual(detail['history'][0]['report_name'], 'Secret')


class TicketTests(MobileTestCase):
    def test_create_list_detail_and_reply(self):
        invalid = self.post_json('tickets', data={'title': 'x', 'description': 'y', 'ticket_type': 'nope'})
        self.assertEqual(invalid.status_code, 400)
        created = self.post_json('tickets', data={
            'title': "Demande d'accès", 'description': 'Accès aux rapports MDM', 'ticket_type': 'access',
        })
        self.assertEqual(created.status_code, 201)
        ticket_id = created.json()['id']
        self.assertEqual(created.json()['ticket_type_label'], "Demande d'accès")

        other = CustomUser.objects.create_user(username='other', role=self.user_role)
        Ticket.objects.create(title='Other', description='x', ticket_type='bug', created_by=other)
        listed = self.client.get(url('tickets'), **self.auth).json()['tickets']
        self.assertEqual([t['id'] for t in listed], [ticket_id])

        reply = self.post_json('ticket_messages', ticket_id, data={'content': 'Merci'})
        self.assertEqual(reply.status_code, 201)
        self.assertTrue(reply.json()['is_mine'])
        detail = self.client.get(url('ticket', ticket_id), **self.auth).json()
        self.assertEqual(detail['messages_count'], 1)
        self.assertEqual(self.client.get(url('ticket', ticket_id), **self.bearer(other)).status_code, 404)
