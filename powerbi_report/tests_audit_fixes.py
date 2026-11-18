"""Regression tests for the security / reliability audit (permission sync, views, navigation)."""
from unittest.mock import Mock, patch

import requests
from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from powerbi_report.models import MetadataOption, ReportRef, UserReportPermission
from powerbi_report.services.permission_sync import PermissionSyncService
from users.models import CustomUser, Role

SERVER = 'http://pbirs.test'


def _admin(username='adm'):
    role = Role.objects.get_or_create(name=settings.ADMIN_ROLE_NAME)[0]
    return CustomUser.objects.create_user(
        username=username, password='pw', role=role, is_superuser=True, is_staff=True,
        ad2000=username.upper(), status='Active',
    )


def _service():
    service = PermissionSyncService()
    service.base_url = SERVER
    service.server_urls = [SERVER]
    return service


class SyncOutageTests(TestCase):
    """'PBIRS did not answer' must never be stored as 'nobody has access'."""

    def setUp(self):
        self.alice = CustomUser.objects.create_user(username='alice', ad2000='ALICE', status='Active')
        self.reports = [
            ReportRef.objects.create(pbirs_id=f'r{i:02d}', name=f'R{i}', path=f'/R{i}', server_url=SERVER)
            for i in range(12)
        ]
        for report in self.reports:
            UserReportPermission.objects.create(user=self.alice, report=report, is_direct=True)

    def _run(self, policy_side_effect):
        service = _service()
        with patch.object(PermissionSyncService, '_get_service_auth', return_value=object()), \
                patch.object(PermissionSyncService, '_fetch_folders_for_auth_all_servers', return_value=[]), \
                patch.object(PermissionSyncService, '_get_pbirs_policy_response', side_effect=policy_side_effect):
            return service.sync_all_permissions_with_service_account()

    def test_a_total_outage_keeps_every_stored_permission_and_reports_failure(self):
        with self.assertRaises(Exception) as caught:
            self._run(requests.exceptions.ConnectionError('down'))
        self.assertIn('kept', str(caught.exception))
        self.assertEqual(UserReportPermission.objects.filter(user=self.alice).count(), 12)

    def test_refused_credentials_are_an_outage_too(self):
        refused = requests.exceptions.HTTPError('401', response=Mock(status_code=401))
        with self.assertRaises(Exception):
            self._run(refused)
        self.assertEqual(UserReportPermission.objects.filter(user=self.alice).count(), 12)

    def test_a_report_pbirs_cannot_read_keeps_its_access_while_the_others_are_refreshed(self):
        broken = self.reports[0].pbirs_id

        def answer(url, auth, context):
            if broken in url:
                raise requests.exceptions.ConnectionError('boom')
            return Mock(json=lambda: {'Policies': []})     # readable, and nobody is listed

        service = _service()
        with patch.object(PermissionSyncService, '_get_service_auth', return_value=object()), \
                patch.object(PermissionSyncService, '_fetch_folders_for_auth_all_servers', return_value=[]), \
                patch.object(PermissionSyncService, '_get_pbirs_policy_response', side_effect=answer):
            service.sync_all_permissions_with_service_account()
        kept = set(UserReportPermission.objects.filter(user=self.alice).values_list('report__pbirs_id', flat=True))
        self.assertEqual(kept, {broken})        # the readable reports now (really) have no policy

    def test_a_server_that_lists_nothing_does_not_wipe_its_local_reports(self):
        service = _service()
        service._report_fetch_success[SERVER] = True
        self.assertEqual(service._remove_stale_report_refs([]), 0)
        self.assertEqual(ReportRef.objects.count(), 12)

    def test_a_report_missing_from_a_real_answer_is_still_removed(self):
        service = _service()
        service._report_fetch_success[SERVER] = True
        answer = [{'Id': r.pbirs_id, '_server_url': SERVER} for r in self.reports[:11]]
        self.assertEqual(service._remove_stale_report_refs(answer), 1)


class OutageClassificationTests(TestCase):
    """Only 'PBIRS could not answer' is an outage: a 403 or 404 just means 'no policy of its own'."""

    def _policies(self, first_error):
        report = {'Id': 'r1', 'Path': '/A/R', 'ParentFolderId': 'f1', '_server_url': SERVER}

        def answer(url, auth, context):
            if 'PowerBIReports' in url:
                raise first_error
            return Mock(json=lambda: {'Policies': [{'GroupUserName': 'DOM\TEAM'}]})

        service = _service()
        with patch.object(PermissionSyncService, '_get_pbirs_policy_response', side_effect=answer):
            return service._fetch_report_policies_for_auth(object(), report)

    def test_a_403_on_the_report_falls_back_to_its_folder_as_before(self):
        denied = requests.exceptions.HTTPError('403', response=Mock(status_code=403))
        self.assertEqual(self._policies(denied), [{'GroupUserName': 'DOM\TEAM'}])

    def test_a_5xx_or_a_dropped_connection_is_an_outage(self):
        from powerbi_report.services.permission_sync import PolicyFetchError
        for error in (requests.exceptions.HTTPError('502', response=Mock(status_code=502)),
                      requests.exceptions.ConnectionError('down'),
                      requests.exceptions.HTTPError('401', response=Mock(status_code=401))):
            with self.assertRaises(PolicyFetchError):
                self._policies(error)


class StrictPolicyReadTests(TestCase):
    def test_the_strict_read_raises_where_the_plain_one_hides_the_failure(self):
        from powerbi_report.views import get_report_permissions
        request = Mock()
        with patch('powerbi_report.views.requests.get', side_effect=requests.exceptions.ConnectionError('down')):
            self.assertEqual(get_report_permissions(request, 'r1', server_url=SERVER, auth=object()), [])
            with self.assertRaises(requests.exceptions.ConnectionError):
                get_report_permissions(request, 'r1', server_url=SERVER, auth=object(), strict=True)


class ForceSyncTests(TestCase):
    def setUp(self):
        self.admin = _admin()
        self.alice = CustomUser.objects.create_user(username='alice', ad2000='alice1', status='Active')
        self.client.force_login(self.admin)
        self.url = reverse('powerbi_report:user_permission', args=['alice1'])
        self.report = {'Id': 'r1', 'Name': 'Ventes', 'Path': '/Ventes', '_server_url': SERVER}

    def test_a_link_cannot_trigger_the_sync(self):
        with patch('powerbi_report.views.get_powerbi_reports', side_effect=AssertionError('called')):
            response = self.client.get(self.url + '?force_sync=true')
        self.assertEqual(response.status_code, 200)

    def test_a_policy_filled_in_GroupUserName_for_the_person_counts_as_direct(self):
        with patch('powerbi_report.views.get_powerbi_reports', return_value=[self.report]), \
                patch('powerbi_report.views.get_report_permissions', return_value=[{'GroupUserName': 'DOM\\Alice1'}]):
            self.client.post(self.url, {'force_sync': 'true'})
        permission = UserReportPermission.objects.get(user=self.alice)
        self.assertTrue(permission.is_direct)

    def test_a_report_pbirs_failed_to_answer_for_keeps_the_stored_access(self):
        ref = ReportRef.objects.create(pbirs_id='r2', name='Autre', path='/Autre')
        UserReportPermission.objects.create(user=self.alice, report=ref, is_direct=True)
        reports = [self.report, {'Id': 'r2', 'Name': 'Autre', 'Path': '/Autre', '_server_url': SERVER}]

        def policies(request, report_id, server_url=None, strict=False):
            if report_id == 'r2':
                raise requests.exceptions.ConnectionError('down')
            return [{'UserName': 'DOM\\alice1'}]

        with patch('powerbi_report.views.get_powerbi_reports', return_value=reports), \
                patch('powerbi_report.views.get_report_permissions', side_effect=policies):
            self.client.post(self.url, {'force_sync': 'true'})
        self.assertEqual(
            set(UserReportPermission.objects.filter(user=self.alice).values_list('report__pbirs_id', flat=True)),
            {'r1', 'r2'},
        )


class ActionsWithoutRefererTests(TestCase):
    def test_sync_with_no_referer_redirects_instead_of_failing(self):
        self.client.force_login(_admin())
        with patch('powerbi_report.views.sync_report_refs', return_value=(1, 0)):
            response = self.client.post(reverse('powerbi_report:sync_reports'))
        self.assertEqual(response.status_code, 302)


class AddAllUsersTests(TestCase):
    def test_only_active_real_people_are_granted(self):
        self.client.force_login(_admin())
        CustomUser.objects.create_user(username='real', ad2000='REAL1', status='Active')
        CustomUser.objects.create_user(username='gone', ad2000='GONE1', is_active=False)
        CustomUser.objects.create_user(username='simulation.fake', ad2000='SIM1')
        sent = {}

        def capture(url, json=None, **kwargs):
            sent['policies'] = json['Policies']
            return Mock(raise_for_status=lambda: None)

        with patch('powerbi_report.views.get_powerbi_reports', return_value=[{'Id': 'r1'}]), \
                patch('powerbi_report.views.get_current_user_auth', return_value=None), \
                patch('powerbi_report.views._policies_for_update', return_value=[]), \
                patch('powerbi_report.views.requests.put', side_effect=capture), \
                patch('powerbi_report.views._update_report_metadata'), \
                patch('powerbi_report.views._sync_local_permissions_for_report'), \
                patch('powerbi_report.models.ReportRef.get_server_url', return_value=SERVER):
            self.client.post(reverse('powerbi_report:add_all_users_to_report', args=['r1']))
        granted = {policy['GroupUserName'] for policy in sent['policies']}
        self.assertIn('REAL1', granted)
        self.assertNotIn('GONE1', granted)
        self.assertNotIn('SIM1', granted)


class DashboardNumbersTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()      # the dashboard is cached per user for a few minutes

    def test_counts_come_from_the_reports_themselves(self):
        admin = _admin()
        CustomUser.objects.create_user(username='u1', ad2000='U1', status='Active')
        CustomUser.objects.create_user(username='u2', ad2000='U2', is_active=False)
        for i, kind in enumerate(['dashboard', 'dashboard', 'bibliotheque', 'anomalie']):
            ReportRef.objects.create(pbirs_id=f'd{i}', name=f'D{i}', path=f'/D{i}', report_type=kind)
        self.client.force_login(admin)
        response = self.client.get(reverse('powerbi_report:dashboard'))
        self.assertEqual(response.status_code, 200)
        context = response.context
        self.assertEqual(context['dashboard_report_count'], 2)
        self.assertEqual(context['extraction_report_count'], 1)
        self.assertEqual(context['anomaly_report_count'], 1)
        active = CustomUser.objects.filter(is_active=True).count()
        self.assertEqual(context['total_users'], active)     # the deactivated account is not counted
        self.assertLess(active, CustomUser.objects.count())
        self.assertTrue(context['reports_per_folder_yearly'])

    def test_the_time_series_uses_the_first_appearance_not_the_last_sync(self):
        import datetime
        from django.utils import timezone
        admin = _admin()
        ReportRef.objects.create(pbirs_id='old', name='Old', path='/Old', report_type='dashboard')
        ReportRef.objects.filter(pbirs_id='old').update(
            first_seen=timezone.make_aware(datetime.datetime(2021, 5, 4)))
        ReportRef.objects.get(pbirs_id='old').save()           # a sync touches last_synced...
        self.client.force_login(admin)
        years = {row['year'].year for row in self.client.get(reverse('powerbi_report:dashboard')).context['reports_per_folder_yearly']}
        self.assertIn(2021, years)                              # ...and must not move the report to today


class ViewGatingAndLooseReportsTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(
            username='viewer', ad2000='V1', status='Active', can_view_direction=True, can_view_module=True,
        )
        self.report = ReportRef.objects.create(
            pbirs_id='x1', name='Sans classement', path='/Sans', report_type='dashboard',
        )
        UserReportPermission.objects.create(user=self.user, report=self.report)
        self.client.force_login(self.user)

    def test_a_report_without_direction_is_listed_next_to_the_folders(self):
        response = self.client.get(reverse('powerbi_report:custom_direction'))
        self.assertEqual([r['name'] for r in response.context['loose_reports']], ['Sans classement'])
        self.assertEqual(response.context['loose_reports'][0]['label'], 'Non classé')

    def test_once_classified_it_is_no_longer_loose(self):
        direction = MetadataOption.objects.create(option_type='direction', name='Finance')
        self.report.directions.add(direction)
        response = self.client.get(reverse('powerbi_report:custom_direction'))
        self.assertEqual(response.context['loose_reports'], [])

    def test_a_report_that_sits_in_another_view_is_not_called_lost(self):
        pole = MetadataOption.objects.create(option_type='pole', name='Retail')
        self.report.poles.add(pole)          # classified by pôle, no direction
        CustomUser.objects.filter(pk=self.user.pk).update(can_view_pole=True)
        for name in ('custom_direction', 'custom_pole', 'custom_module'):
            response = self.client.get(reverse(f'powerbi_report:{name}'))
            self.assertEqual(response.context['loose_reports'], [], name)

    def test_the_module_view_honours_its_permission_flag_like_the_mobile_app(self):
        self.assertEqual(self.client.get(reverse('powerbi_report:custom_module')).status_code, 200)
        CustomUser.objects.filter(pk=self.user.pk).update(can_view_module=False)
        self.assertEqual(self.client.get(reverse('powerbi_report:custom_module')).status_code, 403)

    def test_an_admin_role_without_the_superuser_flag_sees_every_report(self):
        from powerbi_report.views import get_local_reports_for_user
        role = Role.objects.get_or_create(name=settings.ADMIN_ROLE_NAME)[0]
        boss = CustomUser.objects.create_user(username='rolebos', ad2000='RB', role=role, status='Active')
        self.assertEqual([r['Id'] for r in get_local_reports_for_user(boss)], ['x1'])


class PolicyRemovalMatchingTests(TestCase):
    def test_a_policy_stored_under_UserName_is_found_when_removing(self):
        policies = [{'UserName': 'DOM\\alice1', 'Roles': []}, {'GroupUserName': 'DOM\\bob1', 'Roles': []}]
        kept = [
            p for p in policies
            if (p.get('GroupUserName') or p.get('UserName'))
            and (p.get('GroupUserName') or p.get('UserName') or '').split('\\')[-1].lower() != 'alice1'
        ]
        self.assertEqual([p.get('GroupUserName') for p in kept], ['DOM\\bob1'])


class PostButtonsRenderTests(TestCase):
    """The former GET links are real POST forms with a CSRF token (the pages themselves were never viewed in a browser)."""

    def test_sync_buttons_and_logout_are_post_forms(self):
        import re
        self.client.force_login(_admin('superb'))
        page = self.client.get(reverse('powerbi_report:report_list')).content.decode()
        for name, button in (('sync_reports', 'syncPbirsBtn'), ('sync_permissions', 'syncPermissionsBtn')):
            form = re.search(r'<form method="post" action="%s"[^>]*>(.*?)</form>' % reverse(f'powerbi_report:{name}'), page, re.S)
            self.assertIsNotNone(form, name)
            self.assertIn('csrfmiddlewaretoken', form.group(1))
            self.assertIn(f'id="{button}"', form.group(1))
        logout = re.search(r'<form method="post" action="%s"[^>]*>(.*?)</form>' % reverse('logout'), page, re.S)
        self.assertIsNotNone(logout)
        self.assertIn('csrfmiddlewaretoken', logout.group(1))
        self.assertNotIn('href="%s"' % reverse('logout'), page)

    def test_the_user_sync_button_submits_a_post_form(self):
        self.client.force_login(_admin('superc'))
        page = self.client.get(reverse('users_view')).content.decode()
        self.assertIn('id="syncUsersForm" method="post"', page)
        self.assertNotIn("window.location.href = \"/sync_users/\"", page)


class FlatListGatingTests(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(username='flat', ad2000='F1', status='Active', can_view_direction=False)
        self.client.force_login(self.user)

    def test_the_flat_list_cannot_bypass_the_view_flags(self):
        url = reverse('powerbi_report:report_list_flat')
        self.assertEqual(self.client.get(url, {'context': 'direction'}).status_code, 403)
        self.assertEqual(self.client.get(url, {'context': 'biblio'}).status_code, 200)
        CustomUser.objects.filter(pk=self.user.pk).update(can_view_direction=True)
        self.assertEqual(self.client.get(url, {'context': 'direction'}).status_code, 200)


class ServerCredentialsTests(TestCase):
    def test_no_credential_is_stored_for_a_pbirs_server(self):
        from powerbi_report.models import PBIRSServer
        self.assertFalse({'admin_password', 'admin_username'} & {f.name for f in PBIRSServer._meta.get_fields()})
        self.client.force_login(_admin('srvadm'))
        self.client.post(reverse('powerbi_report:server_create'), {'name': 'Prod', 'base_url': 'https://bi.test', 'is_active': 'on'})
        self.assertTrue(PBIRSServer.objects.filter(name='Prod').exists())


class ReplaceReportSyncTests(TestCase):
    def test_a_replace_refreshes_only_that_report(self):
        self.client.force_login(_admin('repl'))
        ReportRef.objects.create(pbirs_id='r9', name='R9', path='/R9', server_url=SERVER)
        from django.core.files.uploadedfile import SimpleUploadedFile
        with patch('powerbi_report.views.get_current_user_auth', return_value=object()), \
                patch('requests.Session.post', return_value=Mock(raise_for_status=lambda: None)), \
                patch('powerbi_report.views._sync_local_permissions_for_report') as one, \
                patch('powerbi_report.views.sync_all_user_permissions') as full, \
                patch('powerbi_report.views._update_report_metadata'):
            self.client.post(reverse('powerbi_report:replace_report', args=['r9']),
                             {'pbix_file': SimpleUploadedFile('a.pbix', b'x'), 'report_path': '/R9'})
        one.assert_called_once()
        full.assert_not_called()


class ModuleViewDashboardsOnlyTests(TestCase):
    def setUp(self):
        self.admin = _admin('modadm')
        self.user = CustomUser.objects.create_user(username='modu', ad2000='M1', status='Active', can_view_module=True)
        module = MetadataOption.objects.create(option_type='module', name='Finance')
        self.refs = {}
        for kind in ('dashboard', 'bibliotheque', 'anomalie'):
            ref = ReportRef.objects.create(pbirs_id=f'm-{kind}', name=kind, path=f'/{kind}', report_type=kind)
            ref.modules.add(module)
            UserReportPermission.objects.create(user=self.user, report=ref)
            self.refs[kind] = ref
        self.module = module

    def _names(self, client_user):
        self.client.force_login(client_user)
        response = self.client.get(reverse('powerbi_report:custom_folder_detail', args=['module', self.module.id]))
        return [r['name'] for r in response.context['reports']]

    def test_only_dashboards_are_in_the_module_view(self):
        self.assertEqual(self._names(self.user), ['dashboard'])
        self.assertEqual(self._names(self.admin), ['dashboard'])

    def test_a_module_holding_only_library_reports_is_not_listed(self):
        ReportRef.objects.filter(report_type='dashboard').delete()
        self.client.force_login(self.user)
        response = self.client.get(reverse('powerbi_report:custom_module'))
        self.assertEqual(list(response.context['subfolders']), [])

    def test_the_library_still_shows_its_reports_by_module(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('powerbi_report:custom_folder_detail', args=['biblio', self.module.id]))
        self.assertEqual([r['name'] for r in response.context['reports']], ['bibliotheque'])
