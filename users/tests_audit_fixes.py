"""Regression tests for the security / reliability audit (accounts, LDAP, roles, audit trail)."""
from unittest.mock import Mock, patch

from django.conf import settings
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from mobile.auth import _client_ip
from users.directory_sync import deactivate_missing_users
from users.ldap_utils import NTLM, connexion_ad2000
from users.models import AdminAuditLog, CustomUser, Role
from users.utils import clean_directory_value


def _admin_role():
    return Role.objects.get_or_create(name=settings.ADMIN_ROLE_NAME)[0]


def _make_admin(username, **extra):
    return CustomUser.objects.create_user(
        username=username, password='pw', role=_admin_role(), is_superuser=True, is_staff=True,
        ad2000=username.upper(), status='Active', **extra,
    )


class DirectoryValuesTests(TestCase):
    def test_a_missing_ldap_attribute_is_no_value(self):
        for raw in (None, '', '[]', 'None', [], ['[]'], '  '):
            self.assertEqual(clean_directory_value(raw), '')
        self.assertEqual(clean_directory_value(['a@b.c']), 'a@b.c')


class WebLoginAccountTests(TestCase):
    def _login(self, username, mail='[]'):
        info = {'username': username, 'email': mail, 'ad2000': username, 'first_name': 'A', 'last_name': 'B'}
        with patch('users.views.connexion_ad2000', return_value=info):
            return self.client.post(reverse('login'), {'username': username, 'password': 'pw'})

    def test_two_people_without_mail_never_share_an_account(self):
        self._login('h000001')
        first = CustomUser.objects.get(username='h000001')
        self.assertEqual(first.email, '')          # "[]" is not stored
        first.role = _admin_role()
        first.is_superuser = True
        first.save()
        self.client.post(reverse('logout'))

        self._login('h000002')
        self.assertEqual(CustomUser.objects.filter(username__in=['h000001', 'h000002']).count(), 2)
        second = CustomUser.objects.get(username='h000002')
        self.assertFalse(second.is_admin)
        self.assertNotEqual(second.pk, first.pk)

    def test_a_shared_mailbox_does_not_merge_accounts(self):
        CustomUser.objects.create_user(username='h000010', email='team@x.test', ad2000='H000010')
        CustomUser.objects.create_user(username='h000011', email='team@x.test', ad2000='H000011')
        self._login('h000012', mail='team@x.test')
        self.assertTrue(CustomUser.objects.filter(username='h000012').exists())

    def test_an_administrator_account_is_never_adopted_through_its_mail(self):
        _make_admin('boss', email='boss@x.test')
        self._login('h000020', mail='boss@x.test')
        self.assertTrue(CustomUser.objects.filter(username='h000020').exists())
        self.assertTrue(CustomUser.objects.get(username='boss').is_admin)

    def test_a_deactivated_account_cannot_sign_in(self):
        CustomUser.objects.create_user(username='h000030', ad2000='H000030', is_active=False)
        response = self._login('h000030')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('_auth_user_id', self.client.session)


class LdapBindTests(TestCase):
    @patch('users.ldap_utils._bind_connection')
    def test_an_email_login_is_an_ntlm_bind_on_the_account_name(self, bind):
        CustomUser.objects.create_user(username='H0000042', email='jean@x.test', ad2000='H0000042')
        bind.return_value = Mock(entries=[])
        connexion_ad2000('JEAN@x.test', 'secret')
        kwargs = bind.call_args.kwargs
        self.assertEqual(kwargs['authentication'], NTLM)
        self.assertTrue(kwargs['user_dn'].endswith('\\H0000042'))

    @patch('users.ldap_utils.lookup_username_by_mail', return_value=None)
    @patch('users.ldap_utils._bind_connection')
    def test_an_unknown_email_is_refused_without_a_clear_text_bind(self, bind, _lookup):
        self.assertIsNone(connexion_ad2000('stranger@elsewhere.test', 'secret'))
        bind.assert_not_called()

    @patch('users.ldap_utils._bind_connection')
    def test_filter_metacharacters_cannot_widen_the_search(self, bind):
        conn = Mock(entries=[])
        bind.return_value = conn
        connexion_ad2000('H1)(sAMAccountName=*', 'secret')
        search_filter = conn.search.call_args.kwargs['search_filter']
        self.assertNotIn(')(', search_filter)
        self.assertIn('\\29', search_filter)

    def test_an_empty_ldap_attribute_is_returned_as_empty_text(self):
        class Attribute:
            value = None

        entry = Mock(sAMAccountName=Mock(value='H1'), mail=Attribute(), name=Mock(value='Jean Dupont'),
                     department=Attribute(), title=Attribute())
        entry.name = Mock(value='Jean Dupont')
        conn = Mock(entries=[entry])
        with patch('users.ldap_utils._bind_connection', return_value=conn):
            info = connexion_ad2000('H1', 'secret')
        self.assertEqual(info['email'], '')
        self.assertEqual(info['department'], '')
        self.assertEqual(info['last_name'], 'Dupont')


class RoleChangeTests(TestCase):
    def setUp(self):
        self.boss = _make_admin('boss1')
        self.other = _make_admin('boss2')
        self.user_role = Role.objects.get_or_create(name=settings.USER_ROLE_NAME)[0]

    def test_taking_the_admin_role_away_takes_the_superuser_rights_away(self):
        self.client.force_login(self.boss)
        self.client.post(reverse('manage_roles'), {'user_id': self.other.id, 'action': 'edit_role', 'new_role': self.user_role.id})
        self.other.refresh_from_db()
        self.assertFalse(self.other.is_superuser)
        self.assertFalse(self.other.is_staff)
        self.assertFalse(self.other.is_admin)

        self.client.post(reverse('user_edit', args=[self.other.id]), {'role': _admin_role().id})
        self.other.refresh_from_db()
        self.assertTrue(self.other.is_superuser)

    def test_removing_the_role_also_demotes(self):
        self.client.force_login(self.boss)
        self.client.post(reverse('manage_roles'), {'user_id': self.other.id, 'action': 'remove_role'})
        self.other.refresh_from_db()
        self.assertIsNone(self.other.role)
        self.assertFalse(self.other.is_superuser)

    def test_the_last_administrator_cannot_be_demoted(self):
        CustomUser.objects.filter(pk=self.other.pk).update(is_active=False)
        self.client.force_login(self.boss)
        self.client.post(reverse('manage_roles'), {'user_id': self.boss.id, 'action': 'edit_role', 'new_role': self.user_role.id})
        self.boss.refresh_from_db()
        self.assertTrue(self.boss.is_admin)


class StateChangesNeedPostTests(TestCase):
    def setUp(self):
        self.admin = _make_admin('boss3')
        self.client.force_login(self.admin)

    def test_get_does_not_log_out_nor_sync(self):
        self.assertEqual(self.client.get(reverse('logout')).status_code, 405)
        self.assertIn('_auth_user_id', self.client.session)
        self.assertEqual(self.client.get(reverse('sync_users')).status_code, 405)
        self.assertEqual(self.client.get(reverse('powerbi_report:sync_reports')).status_code, 405)
        self.assertEqual(self.client.get(reverse('powerbi_report:sync_permissions')).status_code, 405)

    def test_post_logs_out(self):
        response = self.client.post(reverse('logout'))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('_auth_user_id', self.client.session)


class MobileThrottleAddressTests(TestCase):
    def _ip(self, peer, forwarded=None):
        extra = {'REMOTE_ADDR': peer}
        if forwarded:
            extra['HTTP_X_FORWARDED_FOR'] = forwarded
        return _client_ip(RequestFactory().post('/mobile/v1/auth/login/', **extra))

    @override_settings(TRUSTED_PROXY_IPS=['10.0.0.0/24'])
    def test_behind_the_configured_proxy_each_caller_has_its_own_bucket(self):
        self.assertEqual(self._ip('10.0.0.5', '203.0.113.9'), '203.0.113.9')

    @override_settings(TRUSTED_PROXY_IPS=['10.0.0.5'])
    def test_the_part_of_the_header_a_client_wrote_is_ignored(self):
        self.assertEqual(self._ip('10.0.0.5', '6.6.6.6, 203.0.113.9'), '203.0.113.9')

    @override_settings(TRUSTED_PROXY_IPS=['10.0.0.5'])
    def test_a_direct_client_cannot_choose_its_address_even_on_the_lan(self):
        self.assertEqual(self._ip('10.0.0.77', '1.2.3.4'), '10.0.0.77')
        self.assertEqual(self._ip('8.8.8.8', '1.2.3.4'), '8.8.8.8')

    @override_settings(TRUSTED_PROXY_IPS=[])
    def test_without_a_configured_proxy_the_header_is_never_believed(self):
        self.assertEqual(self._ip('10.0.0.5', '203.0.113.9'), '10.0.0.5')

    @override_settings(MOBILE_LOGIN_CLIENT_IP_HEADER='HTTP_X_REAL_IP')
    def test_a_configured_header_wins(self):
        request = RequestFactory().post('/x', REMOTE_ADDR='10.0.0.5', HTTP_X_REAL_IP='198.51.100.7')
        self.assertEqual(_client_ip(request), '198.51.100.7')


class DeactivateMissingUsersTests(TestCase):
    def setUp(self):
        for i in range(20):
            CustomUser.objects.create_user(username=f'h{i:03d}', ad2000=f'H{i:03d}', status='Active')
        self.listed = [{'sAMAccountName': f'h{i:03d}', 'ad2000': f'H{i:03d}'} for i in range(20)]

    def test_an_account_missing_from_the_directory_is_switched_off(self):
        self.assertEqual(deactivate_missing_users(self.listed[:19]), 1)
        self.assertFalse(CustomUser.objects.get(username='h019').is_active)

    def test_a_faulty_answer_changes_nothing(self):
        self.assertEqual(deactivate_missing_users(self.listed[:5]), 0)
        self.assertEqual(CustomUser.objects.filter(is_active=False).count(), 0)

    def test_administrators_are_never_switched_off(self):
        _make_admin('rootadmin')
        self.assertEqual(deactivate_missing_users(self.listed), 0)
        self.assertTrue(CustomUser.objects.get(username='rootadmin').is_active)


class AdminAuditTrailTests(TestCase):
    def test_what_an_admin_changes_is_recorded(self):
        admin = _make_admin('boss4')
        victim = CustomUser.objects.create_user(username='victim', ad2000='V1')
        self.client.force_login(admin)
        self.client.post(reverse('clear_history', args=[victim.id]))
        self.client.get(reverse('user_history'))
        entries = AdminAuditLog.objects.all()
        self.assertEqual(entries.count(), 1)            # only the POST, not the page view
        entry = entries.get()
        self.assertEqual((entry.admin_username, entry.method, entry.view_name), ('boss4', 'POST', 'clear_history'))

    def test_ordinary_users_are_not_logged(self):
        user = CustomUser.objects.create_user(username='plain', ad2000='P1', status='Active')
        self.client.force_login(user)
        self.client.post(reverse('logout'))
        self.assertEqual(AdminAuditLog.objects.count(), 0)


class ProtectedMediaTests(TestCase):
    """Files really exist here: a 404 must come from the guard, not from a missing file."""

    def setUp(self):
        import os
        import tempfile
        from django.test.utils import override_settings as override
        from tickets.models import Ticket

        self.root = tempfile.mkdtemp()
        for folder, name in (('ticket_attachments', 'secret.png'), ('profile_images', 'face.png')):
            os.makedirs(os.path.join(self.root, folder), exist_ok=True)
            with open(os.path.join(self.root, folder, name), 'wb') as handle:
                handle.write(b'x')
        self.override = override(MEDIA_ROOT=self.root)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.owner = CustomUser.objects.create_user(username='owner', ad2000='O1', status='Active')
        self.snoop = CustomUser.objects.create_user(username='snoop', ad2000='S1', status='Active')
        Ticket.objects.create(
            title='t', description='d', ticket_type='bug', category='cbi', priority='low', status='open',
            created_by=self.owner, attachment='ticket_attachments/secret.png',
        )

    def test_the_owner_gets_the_file_and_a_stranger_does_not(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get('/media/ticket_attachments/secret.png').status_code, 200)
        self.client.force_login(self.snoop)
        self.assertEqual(self.client.get('/media/ticket_attachments/secret.png').status_code, 404)
        self.assertEqual(self.client.get('/media/profile_images/face.png').status_code, 200)

    def test_a_dotted_path_cannot_reach_a_private_folder(self):
        self.client.force_login(self.snoop)
        for path in ('profile_images/../ticket_attachments/secret.png', 'profile_images//../ticket_attachments/secret.png',
                     './ticket_attachments/secret.png', 'profile_images%5C..%5Cticket_attachments%5Csecret.png'):
            self.assertEqual(self.client.get('/media/' + path).status_code, 404, path)


class DirectoryLookupTests(TestCase):
    def test_an_admin_whose_local_name_differs_is_found_by_ad2000_and_not_duplicated(self):
        admin = _make_admin('legacy-name')
        CustomUser.objects.filter(pk=admin.pk).update(ad2000='H0099')
        info = {'username': 'h0099', 'email': '', 'ad2000': 'H0099', 'first_name': 'A', 'last_name': 'B'}
        with patch('users.views.connexion_ad2000', return_value=info):
            response = self.client.post(reverse('login'), {'username': 'h0099', 'password': 'pw'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CustomUser.objects.filter(ad2000__iexact='H0099').count(), 1)
        self.assertEqual(int(self.client.session['_auth_user_id']), admin.pk)
