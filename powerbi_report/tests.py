from django.test import TestCase, RequestFactory
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.core.cache import cache
from unittest.mock import patch, Mock
import json
from .views import add_refresh_plan, get_powerbi_reports

User = get_user_model()

class RefreshPlanTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_superuser(username='testuser', password='password')
        self.report_id = 'test-report-id'

    @patch('powerbi_report.views.get_current_user_auth')
    @patch('powerbi_report.views.requests.post')
    @patch('powerbi_report.views.get_powerbi_report_info')
    def test_add_refresh_plan_monthly_comma(self, mock_get_info, mock_post, mock_auth):
        """Test Monthly recurrence with '1, 15' input -> expects '1,15' payload"""
        # Mock auth and response
        mock_auth.return_value = ('user', 'pass')
        mock_response = Mock()
        mock_response.status_code = 201
        mock_post.return_value = mock_response

        # Prepare POST data
        data = {
            'description': 'Test Plan',
            'catalog_item_path': '/Path/To/Report',
            'plan_type': 'specific',
            'recurrence_type': 'Month',
            'start_datetime': '2026-02-03T12:00',
            'month_days': '1, 15', # User input with space
            'months': ['January', 'February']
        }

        # Create request
        request = self.factory.post(reverse('powerbi_report:add_refresh_plan', args=[self.report_id]), data)
        request.user = self.user
        request._messages = Mock() # Mock messages framework

        # Execute view
        add_refresh_plan(request, self.report_id)

        # Verify payload
        args, kwargs = mock_post.call_args
        payload = json.loads(kwargs['data'])
        
        self.assertEqual(payload['Schedule']['Definition']['Recurrence']['MonthlyRecurrence']['Days'], '1,15')
        self.assertEqual(payload['Schedule']['Definition']['Recurrence']['MonthlyRecurrence']['MonthsOfYear']['January'], True)
        self.assertEqual(payload['Schedule']['Definition']['Recurrence']['MonthlyRecurrence']['MonthsOfYear']['March'], False)

    @patch('powerbi_report.views.get_current_user_auth')
    @patch('powerbi_report.views.requests.post')
    @patch('powerbi_report.views.get_powerbi_report_info')
    def test_add_refresh_plan_monthly_range(self, mock_get_info, mock_post, mock_auth):
        """Test Monthly recurrence with '1-25' input -> expects '1-25' payload"""
        # Mock auth and response
        mock_auth.return_value = ('user', 'pass')
        mock_response = Mock()
        mock_response.status_code = 201
        mock_post.return_value = mock_response

        # Prepare POST data
        data = {
            'description': 'Test Plan Range',
            'catalog_item_path': '/Path/To/Report',
            'plan_type': 'specific',
            'recurrence_type': 'Month',
            'start_datetime': '2026-02-03T12:00',
            'month_days': '1-25', # User input range
            'months': ['January']
        }

        # Create request
        request = self.factory.post(reverse('powerbi_report:add_refresh_plan', args=[self.report_id]), data)
        request.user = self.user
        request._messages = Mock() 

        # Execute view
        add_refresh_plan(request, self.report_id)

        # Verify payload
        args, kwargs = mock_post.call_args
        payload = json.loads(kwargs['data'])
        
        self.assertEqual(payload['Schedule']['Definition']['Recurrence']['MonthlyRecurrence']['Days'], '1-25')


class GetPowerBIReportsTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(username='report_user', password='password')
        cache.clear()

    def _request(self):
        request = self.factory.get('/')
        request.user = self.user
        return request

    @patch('powerbi_report.views.get_current_user_auth')
    @patch('powerbi_report.views.requests.Session')
    def test_default_endpoint_fetches_reports_only(self, mock_session_cls, mock_auth):
        mock_auth.return_value = ('user', 'pass')

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'value': [{'Id': 'r1', 'Name': 'Report 1', 'Path': '/A/Report1'}]
        }

        mock_session = Mock()
        mock_session.get.return_value = mock_response
        mock_session_cls.return_value = mock_session

        reports = get_powerbi_reports(self._request())

        called_url = mock_session.get.call_args.kwargs['url']
        self.assertTrue(called_url.endswith('/Reports/api/v2.0/PowerBIReports'))
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]['Id'], 'r1')
        self.assertEqual(reports[0]['Type'], 'PowerBIReport')

    @patch('powerbi_report.views.get_current_user_auth')
    @patch('powerbi_report.views.requests.Session')
    def test_catalog_items_filters_out_folders(self, mock_session_cls, mock_auth):
        mock_auth.return_value = ('user', 'pass')

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'value': [
                {'Id': 'f1', 'Name': 'Folder A', 'Type': 'Folder'},
                {'Id': 'r1', 'Name': 'Report A', 'Type': 'PowerBIReport'},
                {'Id': 'f2', 'Name': 'Folder B', 'Type': 1},
                {'Id': 'r2', 'Name': 'Report B', 'Type': 13},
            ]
        }

        mock_session = Mock()
        mock_session.get.return_value = mock_response
        mock_session_cls.return_value = mock_session

        reports = get_powerbi_reports(self._request(), endpoint='CatalogItems')

        self.assertEqual([item['Id'] for item in reports], ['r1', 'r2'])
        self.assertTrue(all(item['Type'] == 'PowerBIReport' for item in reports))

    @patch('powerbi_report.views.get_current_user_auth')
    @patch('powerbi_report.views.requests.Session')
    def test_cached_empty_list_is_respected(self, mock_session_cls, mock_auth):
        user_id = self.user.id
        cache_key = f'powerbi_reports_cache_{user_id}_PowerBIReports'
        cache.set(cache_key, [], timeout=200)

        reports = get_powerbi_reports(self._request())

        self.assertEqual(reports, [])
        mock_session_cls.assert_not_called()


class SharedLinkLoginTests(TestCase):
    """A shared report link must send anonymous users to login, then back."""

    def test_anonymous_shared_link_redirects_to_login_with_next(self):
        url = reverse('powerbi_report:embed_report', kwargs={'report_path': 'CBI/Ventes'})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, f"{reverse('login')}?next={url}")

    def test_login_returns_to_next_and_rejects_external_targets(self):
        User.objects.create_user(username='share_user', password='password')
        with patch('users.views.connexion_ad2000', return_value=None):
            response = self.client.post(
                reverse('login'),
                {'username': 'share_user', 'password': 'password', 'next': '/powerbi/embed/CBI/Ventes/'},
            )
        self.assertEqual(response.url, '/powerbi/embed/CBI/Ventes/')

        self.client.logout()
        with patch('users.views.connexion_ad2000', return_value=None):
            response = self.client.post(
                reverse('login'),
                {'username': 'share_user', 'password': 'password', 'next': 'https://evil.example/'},
            )
        self.assertEqual(response.url, reverse('home'))


class PermissionChangeTests(TestCase):
    def setUp(self):
        from powerbi_report.models import ReportRef
        self.admin = User.objects.create_superuser(username='perm_admin', password='password')
        self.target = User.objects.create_user(username='perm_target', password='password', ad2000='target')
        self.report = ReportRef.objects.create(pbirs_id='rep-1', name='Ventes', path='/CBI/Ventes')
        self.client.force_login(self.admin)

    @patch('powerbi_report.views._sync_local_permissions_for_report')
    @patch('powerbi_report.views.get_powerbi_report_info', return_value=None)
    @patch('powerbi_report.views._policies_for_update', return_value=[])
    @patch('powerbi_report.views.get_current_user_auth', return_value=('u', 'p'))
    @patch('powerbi_report.views.requests.put')
    def test_grant_succeeds_when_report_info_lookup_fails(self, mock_put, *_mocks):
        mock_put.return_value = Mock(raise_for_status=Mock())
        response = self.client.post(reverse('powerbi_report:add_users_to_report', args=['rep-1', 'target']))
        self.assertEqual(response.status_code, 302)
        from notifications.models import Notification
        self.assertTrue(Notification.objects.filter(user=self.target, message__contains='Ventes').exists())

    @patch('powerbi_report.views.get_current_user_auth', return_value=('u', 'p'))
    @patch('powerbi_report.views.requests.put')
    def test_nothing_is_written_when_current_policies_cannot_be_read(self, mock_put, _auth):
        """A failed read must not be written back as an (almost) empty access list."""
        from powerbi_report.services.pbirs_client import PBIRSConnectionError
        routes = [
            ('add_users_to_report', ['rep-1', 'target'], {}),
            ('remove_user', ['rep-1', 'target'], {}),
            ('add_selected_users_to_report', ['rep-1'], {'selected_users': ['target']}),
            ('remove_selected_users_from_report', ['rep-1'], {'selected_users': ['target']}),
            ('add_permission_to_server', ['rep-1', 'target'], {}),
            ('remove_permission_from_server', ['rep-1', 'target'], {}),
            ('add_selected_permissions', ['target'], {'report_ids': ['rep-1']}),
            ('remove_selected_permissions', ['target'], {'report_ids': ['rep-1']}),
        ]
        with patch('powerbi_report.views.PBIRSClient') as pbirs:
            pbirs.return_value.get_report_policies_strict.side_effect = PBIRSConnectionError('down')
            for name, args, data in routes:
                with self.subTest(route=name):
                    response = self.client.post(reverse(f'powerbi_report:{name}', args=args), data)
                    self.assertEqual(response.status_code, 302)
        mock_put.assert_not_called()


class MetadataSelectAllTests(TestCase):
    def test_selecting_every_societe_saves(self):
        from powerbi_report.models import ReportRef, MetadataOption
        admin = User.objects.create_superuser(username='meta_admin', password='password')
        report = ReportRef.objects.create(pbirs_id='rep-meta', name='R', path='/CBI/R')
        societes = [
            MetadataOption.objects.create(option_type='societe', name=f'Société avec un nom assez long {i}')
            for i in range(12)
        ]
        self.client.force_login(admin)
        response = self.client.post(
            reverse('powerbi_report:update_report_metadata', args=['rep-meta']),
            {'societes': [s.id for s in societes], 'report_type': 'dashboard'},
        )
        self.assertEqual(response.status_code, 302)
        report.refresh_from_db()
        self.assertEqual(report.societes.count(), 12)
        self.assertGreater(len(report.societe), 128)


class SliderTests(TestCase):
    def setUp(self):
        from powerbi_report.models import ReportRef, UserReportPermission
        self.user = User.objects.create_user(username='slider_user', password='password', can_use_slider=True)
        self.other = User.objects.create_user(username='slider_other', password='password', can_use_slider=True)
        self.r1 = ReportRef.objects.create(pbirs_id='s-1', name='Ventes', path='/CBI/Ventes', embed_url='http://pbirs/Reports/powerbi/CBI/Ventes?rs:embed=true')
        self.r2 = ReportRef.objects.create(pbirs_id='s-2', name='Achats', path='/CBI/Achats')
        self.secret = ReportRef.objects.create(pbirs_id='s-3', name='Secret', path='/CBI/Secret')
        for report in (self.r1, self.r2):
            UserReportPermission.objects.create(user=self.user, report=report)
        self.client.force_login(self.user)

    def _create(self, report_ids, name='Mur'):
        return self.client.post(reverse('powerbi_report:slider_create'), {
            'name': name, 'interval_seconds': '2', 'reports': [str(i) for i in report_ids],
        })

    def test_requires_slider_permission(self):
        self.user.can_use_slider = False
        self.user.save()
        self.assertEqual(self.client.get(reverse('powerbi_report:slider_list')).status_code, 403)

    def test_create_keeps_order_clamps_interval_and_drops_inaccessible(self):
        from powerbi_report.models import Slider
        self._create([self.r2.id, self.secret.id, self.r1.id, self.r2.id])
        slider = Slider.objects.get(owner=self.user)
        self.assertEqual([i.report_id for i in slider.items.all()], [self.r2.id, self.r1.id])
        self.assertEqual(slider.interval_seconds, 5)

    def test_the_list_offers_play_only(self):
        self._create([self.r1.id])
        page = self.client.get(reverse('powerbi_report:slider_list'))
        self.assertContains(page, 'Play')
        self.assertNotContains(page, 'Présenter')
        self.assertNotContains(page, 'Kiosque')

    def test_other_users_cannot_play_edit_or_delete(self):
        from powerbi_report.models import Slider
        self._create([self.r1.id])
        slider = Slider.objects.get(owner=self.user)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(reverse('powerbi_report:slider_play', args=[slider.id])).status_code, 404)
        self.assertEqual(self.client.get(reverse('powerbi_report:slider_edit', args=[slider.id])).status_code, 404)
        self.client.post(reverse('powerbi_report:slider_delete', args=[slider.id]))
        self.assertTrue(Slider.objects.filter(id=slider.id).exists())

    def test_play_skips_reports_whose_access_was_lost_and_logs_history(self):
        from powerbi_report.models import Slider, UserReportPermission
        from users.models import UserHistory
        self._create([self.r1.id, self.r2.id])
        slider = Slider.objects.get(owner=self.user)
        UserReportPermission.objects.filter(user=self.user, report=self.r2).delete()
        response = self.client.get(reverse('powerbi_report:slider_play', args=[slider.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([s['name'] for s in response.context['slides']], ['Ventes'])
        self.assertEqual(response.context['skipped'], 1)
        self.assertEqual(response.context['mode'], 'kiosk')
        self.assertTrue(UserHistory.objects.filter(user=self.user, action__startswith='Rapport consulté (slider)').exists())


def _cutoff_yesterday():
    """The feature went live yesterday: accounts created from now on are 'new'."""
    from datetime import timedelta
    from django.utils import timezone
    from powerbi_report.models import OdooSettings
    OdooSettings.objects.update_or_create(pk=1, defaults={'new_users_since': timezone.now() - timedelta(days=1)})


def _old_account(user):
    """Make an account look like it existed before the feature went live."""
    from datetime import timedelta
    from django.utils import timezone
    User.objects.filter(pk=user.pk).update(date_joined=timezone.now() - timedelta(days=30))
    user.refresh_from_db()
    return user


class NewUserTests(TestCase):
    """People in BOTH the directory and Odoo are announced to the admins; nothing is granted."""

    def setUp(self):
        _cutoff_yesterday()
        self.admin = User.objects.create_superuser(username='nu_admin', password='x', email='admin@example.com')

    def _emp(self, employee_id, name, title='Ingénieur', company='Giryad', phone='0661 00 00 00'):
        from powerbi_report.services.odoo_client import OdooEmployee
        return OdooEmployee(employee_id=str(employee_id), full_name=name, post_title=title, hire_date=None,
                            company=company, phone=phone)

    def test_name_matching_ignores_accents_case_and_order(self):
        from powerbi_report.services.odoo_sync import name_key
        self.assertEqual(name_key('DUPONT Jean-Éric'), name_key('jean eric dupont'))

    def test_a_person_in_both_systems_is_flagged_and_their_odoo_profile_stored(self):
        from powerbi_report.models import NewUserAlert, OdooProfile
        from powerbi_report.services.odoo_sync import sync_odoo_profiles
        by_id = User.objects.create_user(username='h0001', password='x', ad2000='H0001')
        by_name = User.objects.create_user(username='amel', password='x', ad2000='x9', first_name='Amel', last_name='Benali')
        summary = sync_odoo_profiles([self._emp('h0001', 'Someone Else'), self._emp(77, 'BENALI amel', title='Juriste')])
        self.assertEqual((summary.matched, summary.new_users), (2, 2))
        self.assertEqual(NewUserAlert.objects.count(), 2)
        profile = OdooProfile.objects.get(user=by_name)
        self.assertEqual((profile.title, profile.company, profile.phone), ('Juriste', 'Giryad', '0661 00 00 00'))
        self.assertTrue(OdooProfile.objects.filter(user=by_id).exists())

    def test_someone_in_only_one_system_is_not_flagged(self):
        from powerbi_report.models import NewUserAlert
        from powerbi_report.services.odoo_sync import sync_odoo_profiles
        User.objects.create_user(username='ldaponly', password='x', ad2000='L1', first_name='Lina', last_name='Seule')
        summary = sync_odoo_profiles([self._emp('Z9', 'Personne Inconnue')])  # in Odoo, not in CBI
        self.assertEqual((summary.matched, summary.unmatched, summary.new_users), (0, 1, 0))
        self.assertFalse(NewUserAlert.objects.exists())

    def test_existing_accounts_get_a_profile_but_are_never_announced(self):
        from powerbi_report.models import NewUserAlert, OdooProfile
        from powerbi_report.services.odoo_sync import sync_odoo_profiles
        old = _old_account(User.objects.create_user(username='old', password='x', ad2000='old1'))
        summary = sync_odoo_profiles([self._emp('old1', 'Old One')])
        self.assertEqual((summary.matched, summary.new_users), (1, 0))
        self.assertTrue(OdooProfile.objects.filter(user=old).exists())
        self.assertFalse(NewUserAlert.objects.exists())

    def test_a_person_is_announced_once_and_stays_seen(self):
        from powerbi_report.models import NewUserAlert
        from powerbi_report.services.odoo_sync import sync_odoo_profiles
        user = User.objects.create_user(username='once', password='x', ad2000='o1')
        sync_odoo_profiles([self._emp('o1', 'Once One')])
        alert = NewUserAlert.objects.get(user=user)
        alert.seen_by, alert.seen_at = self.admin, alert.detected_at
        alert.save()
        again = sync_odoo_profiles([self._emp('o1', 'Once One', title='Autre poste')])
        self.assertEqual(again.new_users, 0)
        self.assertEqual(NewUserAlert.objects.count(), 1)
        self.assertIsNotNone(NewUserAlert.objects.get().seen_at)

    def test_homonyms_are_never_guessed(self):
        from powerbi_report.models import NewUserAlert
        from powerbi_report.services.odoo_sync import sync_odoo_profiles
        User.objects.create_user(username='a', password='x', ad2000='a1', first_name='Karim', last_name='Saidi')
        User.objects.create_user(username='b', password='x', ad2000='b1', first_name='Karim', last_name='Saidi')
        summary = sync_odoo_profiles([self._emp(5, 'Karim Saidi')])
        self.assertEqual((summary.matched, summary.ambiguous), (0, 1))
        self.assertFalse(NewUserAlert.objects.exists())

    def test_the_sync_never_grants_any_access(self):
        from powerbi_report.models import ReportRef, UserReportPermission
        from powerbi_report.services.odoo_sync import sync_odoo_profiles
        ReportRef.objects.create(pbirs_id='r-1', name='Ventes', path='/Ventes')
        User.objects.create_user(username='nogrant', password='x', ad2000='g1')
        with patch('powerbi_report.services.pbirs_client.PBIRSClient') as pbirs:
            sync_odoo_profiles([self._emp('g1', 'No Grant')])
        pbirs.assert_not_called()
        self.assertFalse(UserReportPermission.objects.exists())

    def test_daily_digest_tells_each_admin_once_a_day_with_a_link_and_only_when_someone_is_new(self):
        from io import StringIO
        from django.core.management import call_command
        from notifications.models import Notification
        from powerbi_report.models import NewUserAlert
        other_admin = User.objects.create_superuser(username='nu_admin2', password='x')
        newcomer = User.objects.create_user(username='new', password='x', ad2000='n1')
        out = StringIO()
        call_command('new_users_digest', stdout=out)
        self.assertIn('Nobody new', out.getvalue())
        NewUserAlert.objects.create(user=newcomer)
        call_command('new_users_digest', stdout=StringIO())
        call_command('new_users_digest', stdout=StringIO())  # same day: no second notification
        for admin in (self.admin, other_admin):
            notice = Notification.objects.get(user=admin)
            self.assertEqual(notice.link, reverse('new_users'))
            self.assertIn('1 personne', notice.message)
        self.assertFalse(Notification.objects.filter(user=newcomer).exists())
        NewUserAlert.objects.update(seen_at=timezone_now())
        Notification.objects.all().delete()
        call_command('new_users_digest', stdout=StringIO())
        self.assertFalse(Notification.objects.exists())

    def test_the_list_shows_title_direction_and_company_and_is_for_admins_only(self):
        from powerbi_report.models import NewUserAlert, OdooProfile
        newcomer = User.objects.create_user(username='lst', password='x', ad2000='LST1', first_name='Lila', last_name='Test',
                                            direction='Direction Finance', societe='GSHA')
        OdooProfile.objects.create(user=newcomer, employee_id='LST1', title='Contrôleur de gestion',
                                   title_key='controleur de gestion', company='Giryad', company_key='giryad')
        NewUserAlert.objects.create(user=newcomer)
        self.client.force_login(self.admin)
        page = self.client.get(reverse('new_users'))
        self.assertEqual(page.status_code, 200)
        for text in ('Lila Test', 'Contrôleur de gestion', 'Direction Finance', 'Giryad'):
            self.assertContains(page, text)
        self.assertContains(page, reverse('powerbi_report:missing_permissions', args=['LST1']))
        self.client.force_login(newcomer)
        self.assertEqual(self.client.get(reverse('new_users')).status_code, 403)

    def test_a_company_falls_back_to_the_directory_when_odoo_has_none(self):
        from powerbi_report.models import NewUserAlert
        newcomer = User.objects.create_user(username='fb', password='x', ad2000='FB1', societe='GSHA')
        NewUserAlert.objects.create(user=newcomer)
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse('new_users')), 'GSHA')

    def test_marking_people_as_seen_moves_them_out_of_the_waiting_list(self):
        from powerbi_report.models import NewUserAlert
        a = NewUserAlert.objects.create(user=User.objects.create_user(username='sa', password='x', ad2000='SA1'))
        b = NewUserAlert.objects.create(user=User.objects.create_user(username='sb', password='x', ad2000='SB1'))
        self.client.force_login(self.admin)
        self.client.post(reverse('new_user_seen'), {'alerts': [a.id]})
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((a.seen_at is not None, a.seen_by, b.seen_at), (True, self.admin, None))
        self.client.post(reverse('new_user_seen'), {'all': '1'})
        b.refresh_from_db()
        self.assertIsNotNone(b.seen_at)
        self.assertEqual(len(self.client.get(reverse('new_users')).context['waiting']), 0)

    def test_a_notification_with_a_link_opens_that_page(self):
        from notifications.models import Notification
        Notification.objects.create(user=self.admin, message='Nouveaux utilisateurs : 1 personne à examiner.', link=reverse('new_users'))
        self.client.force_login(self.admin)
        page = self.client.get(reverse('notifications'))
        self.assertContains(page, f'href="{reverse("new_users")}"')

    def test_user_details_shows_the_latest_odoo_info_once_and_the_phone(self):
        from powerbi_report.models import OdooProfile
        user = User.objects.create_user(username='dt', password='x', ad2000='d1', first_name='Dina', last_name='Test', societe='GSHA')
        OdooProfile.objects.create(user=user, employee_id='D0001', title='Juriste', title_key='juriste',
                                   company='Giryad', company_key='giryad', phone='0555 12 34 56')
        url = reverse('user_details')
        self.client.force_login(self.admin)
        page = self.client.get(url, {'user_id': user.id})
        self.assertEqual(page.status_code, 200)
        body = page.content.decode()
        for text in ('Juriste', 'Giryad', 'D0001', '0555 12 34 56'):
            self.assertIn(text, body)
        self.assertEqual(body.count('Juriste'), 1)       # each fact appears once
        self.assertNotIn('>GSHA<', body)                 # the Odoo company wins over the directory one
        self.assertNotIn('>Pôle</dt>', body)
        self.assertNotIn('Groupes métier', body)
        OdooProfile.objects.all().delete()
        without = self.client.get(url, {'user_id': user.id})
        self.assertEqual(without.status_code, 200)
        self.assertContains(without, 'GSHA')              # falls back to the directory company
        self.assertNotContains(without, 'mis à jour depuis Odoo')

    def test_command_does_nothing_when_odoo_is_not_configured(self):
        from io import StringIO
        from django.core.management import call_command
        from django.test import override_settings
        out = StringIO()
        with override_settings(ODOO_DB_HOST=''):
            call_command('sync_odoo_profiles', stdout=out)
        self.assertIn('not configured', out.getvalue())


def timezone_now():
    from django.utils import timezone
    return timezone.now()


class ManualRlsTests(TestCase):
    """RLS is per report and assigned by an admin: roles are read from PBIRS, never guessed."""

    def setUp(self):
        from powerbi_report.models import ReportRef
        # Two reports with different, non-company role sets.
        self.sales = ReportRef.objects.create(pbirs_id='rls-1', name='Ventes', path='/Ventes', server_url='http://pbirs')
        self.hr = ReportRef.objects.create(pbirs_id='rls-2', name='RH', path='/RH', server_url='http://pbirs')
        self.alger, self.oran, self.dg = 'r-alger', 'r-oran', 'r-dg'
        self.sales_roles = [{'ModelRoleId': self.alger, 'ModelRoleName': 'Wilaya Alger'},
                            {'ModelRoleId': self.oran, 'ModelRoleName': 'Wilaya Oran'}]
        self.hr_roles = [{'ModelRoleId': self.dg, 'ModelRoleName': 'Direction Generale'}]
        self.admin = User.objects.create_superuser(username='rls_admin', password='x')
        self.emp = User.objects.create_user(username='e', password='x', ad2000='e1', first_name='Eve', last_name='Em')

    def _client(self, roles, assignments):
        client = Mock()
        client.get_data_model_roles_strict.return_value = roles
        client.get_role_assignments_strict.return_value = assignments
        return client

    def test_assigning_keeps_everyone_else_and_adds_to_existing_users(self):
        from powerbi_report.services import rls
        existing = [{'GroupUserName': 'DOM\\other', 'DataModelRoles': [self.alger]},
                    {'GroupUserName': 'DOM\\e1', 'DataModelRoles': [self.alger]}]
        client = self._client(self.sales_roles, [dict(a) for a in existing])
        added = rls.assign_roles(client, self.sales, ['e1', 'new1'], [self.alger, self.oran])
        sent = client.set_role_assignments_strict.call_args[0][1]
        by_user = {a['GroupUserName'].split('\\')[-1]: a['DataModelRoles'] for a in sent}
        self.assertEqual(by_user, {'other': [self.alger], 'e1': [self.alger, self.oran], 'new1': [self.alger, self.oran]})
        self.assertEqual(added, 3)

    def test_roles_follow_the_style_pbirs_already_uses(self):
        from powerbi_report.services import rls
        by_name = self._client(self.sales_roles, [{'GroupUserName': 'DOM\\other', 'DataModelRoles': ['Wilaya Alger']}])
        rls.assign_roles(by_name, self.sales, ['e1'], [self.oran])
        sent = by_name.set_role_assignments_strict.call_args[0][1]
        self.assertEqual(sent[-1]['DataModelRoles'], ['Wilaya Oran'])
        by_id = self._client(self.sales_roles, [])
        rls.assign_roles(by_id, self.sales, ['e1'], [self.oran])
        self.assertEqual(by_id.set_role_assignments_strict.call_args[0][1][-1]['DataModelRoles'], [self.oran])

    def test_real_pbirs_response_shape_roles_by_id_and_domain_qualified_user(self):
        """Captured from the RLS_test report on the production PBIRS."""
        from powerbi_report.services import rls
        vip = '9e009e4f-9c35-4400-bae0-acb142e30c94'
        gryd = 'b454f70a-9f73-463d-8c7a-e4091804657d'
        roles = [{'ModelRoleId': vip, 'ModelRoleName': 'VIP'}, {'ModelRoleId': gryd, 'ModelRoleName': 'GRYD'}]
        existing = [{'GroupUserName': 'GROUPE-HASNAOUI\\H0017549', 'DataModelRoles': [vip]}]
        client = self._client(roles, existing)
        rls.assign_roles(client, self.sales, ['H0017549', 'x1'], [gryd])
        sent = client.set_role_assignments_strict.call_args[0][1]
        self.assertEqual(
            [(a['GroupUserName'], a['DataModelRoles']) for a in sent],
            [('GROUPE-HASNAOUI\\H0017549', [vip, gryd]), ('GROUPE-HASNAOUI\\x1', [gryd])],
        )

    def test_a_role_of_another_report_is_refused_and_nothing_is_written(self):
        from powerbi_report.services import rls
        from powerbi_report.services.pbirs_client import PBIRSClientError
        client = self._client(self.sales_roles, [])
        with self.assertRaises(PBIRSClientError):
            rls.assign_roles(client, self.sales, ['e1'], [self.dg])
        client.set_role_assignments_strict.assert_not_called()

    def test_removing_one_role_keeps_the_other_and_other_users(self):
        from powerbi_report.services import rls
        client = self._client(self.sales_roles, [
            {'GroupUserName': 'DOM\\e1', 'DataModelRoles': [self.alger, self.oran]},
            {'GroupUserName': 'DOM\\keep', 'DataModelRoles': [self.alger]}])
        self.assertTrue(rls.remove_role(client, self.sales, 'DOM\\e1', self.alger))
        sent = client.set_role_assignments_strict.call_args[0][1]
        self.assertEqual({a['GroupUserName']: a['DataModelRoles'] for a in sent},
                         {'DOM\\e1': [self.oran], 'DOM\\keep': [self.alger]})
        client = self._client(self.sales_roles, [{'GroupUserName': 'DOM\\e1', 'DataModelRoles': [self.alger]}])
        rls.remove_role(client, self.sales, 'e1', self.alger)
        self.assertEqual(client.set_role_assignments_strict.call_args[0][1], [])

    @patch('powerbi_report.views.PBIRSClient')
    @patch('powerbi_report.views.requests.get', side_effect=__import__('requests').RequestException)
    @patch('powerbi_report.views.get_report_permissions')
    @patch('powerbi_report.views.get_powerbi_reports')
    def test_permissions_page_shows_each_entrys_rls_roles(self, reports, policies, _ldap, client_cls):
        reports.return_value = [{'Id': self.sales.pbirs_id, 'Name': 'Ventes', 'Path': '/Ventes'}]
        policies.return_value = [{'GroupUserName': 'DOM\\e1', 'Roles': []}, {'GroupUserName': 'DOM\\nobody', 'Roles': []}]
        client = client_cls.return_value
        client.get_data_model_roles_strict.return_value = self.sales_roles
        client.get_role_assignments_strict.return_value = [{'GroupUserName': 'DOM\\e1', 'DataModelRoles': [self.alger, self.oran]}]
        self.client.force_login(self.admin)
        page = self.client.get(reverse('powerbi_report:report_permissions', args=[self.sales.pbirs_id]))
        self.assertEqual(page.status_code, 200)
        by_id = {p['Identifier']: p for p in page.context['policies']}
        self.assertEqual(by_id['e1']['RlsRoles'], ['Wilaya Alger', 'Wilaya Oran'])
        self.assertEqual(by_id['e1']['RlsRoleIds'], f'{self.alger},{self.oran}')
        self.assertEqual([r['name'] for r in page.context['rls_roles']], ['Wilaya Alger', 'Wilaya Oran'])
        self.assertContains(page, 'rls-edit-btn')
        self.assertEqual(by_id['nobody']['RlsRoles'], [])
        self.assertContains(page, 'Wilaya Alger')
        self.assertContains(page, 'Aucun rôle')

    @patch('powerbi_report.views.PBIRSClient')
    @patch('powerbi_report.views.requests.get', side_effect=__import__('requests').RequestException)
    @patch('powerbi_report.views.get_report_permissions')
    @patch('powerbi_report.views.get_powerbi_reports')
    def test_permissions_page_still_loads_when_pbirs_roles_are_unavailable(self, reports, policies, _ldap, client_cls):
        from powerbi_report.services.pbirs_client import PBIRSClientError
        reports.return_value = [{'Id': self.sales.pbirs_id, 'Name': 'Ventes', 'Path': '/Ventes'}]
        policies.return_value = [{'GroupUserName': 'DOM\\e1', 'Roles': []}]
        client_cls.return_value.get_data_model_roles_strict.side_effect = PBIRSClientError('down')
        self.client.force_login(self.admin)
        page = self.client.get(reverse('powerbi_report:report_permissions', args=[self.sales.pbirs_id]))
        self.assertEqual(page.status_code, 200)
        self.assertFalse(page.context['rls_available'])
        self.assertNotContains(page, 'Aucun rôle')

    def test_set_user_roles_replaces_the_users_set_and_keeps_others(self):
        from powerbi_report.services import rls
        client = self._client(self.sales_roles, [
            {'GroupUserName': 'DOM\\e1', 'DataModelRoles': [self.alger]},
            {'GroupUserName': 'DOM\\keep', 'DataModelRoles': [self.alger]}])
        self.assertTrue(rls.set_user_roles(client, self.sales, 'e1', [self.oran]))  # swap Alger for Oran
        sent = client.set_role_assignments_strict.call_args[0][1]
        self.assertEqual({a['GroupUserName']: a['DataModelRoles'] for a in sent},
                         {'DOM\\keep': [self.alger], 'DOM\\e1': [self.oran]})

    def test_set_user_roles_with_nothing_ticked_removes_the_user_and_no_change_writes_nothing(self):
        from powerbi_report.services import rls
        client = self._client(self.sales_roles, [{'GroupUserName': 'DOM\\e1', 'DataModelRoles': [self.alger, self.oran]}])
        self.assertFalse(rls.set_user_roles(client, self.sales, 'e1', [self.oran, self.alger]))
        client.set_role_assignments_strict.assert_not_called()
        self.assertTrue(rls.set_user_roles(client, self.sales, 'e1', []))
        self.assertEqual(client.set_role_assignments_strict.call_args[0][1], [])

    def test_set_user_roles_adds_a_new_user_and_refuses_roles_of_another_report(self):
        from powerbi_report.services import rls
        from powerbi_report.services.pbirs_client import PBIRSClientError
        client = self._client(self.sales_roles, [])
        rls.set_user_roles(client, self.sales, 'e1', [self.alger, self.oran])
        sent = client.set_role_assignments_strict.call_args[0][1]
        self.assertEqual([(a['GroupUserName'].split('\\')[-1], a['DataModelRoles']) for a in sent], [('e1', [self.alger, self.oran])])
        client.set_role_assignments_strict.reset_mock()
        with self.assertRaises(PBIRSClientError):
            rls.set_user_roles(client, self.sales, 'e1', [self.dg])
        client.set_role_assignments_strict.assert_not_called()

    @patch('powerbi_report.views.PBIRSClient')
    def test_save_from_the_permissions_page(self, client_cls):
        from powerbi_report.models import UserReportPermission
        UserReportPermission.objects.create(user=self.emp, report=self.sales)
        client = client_cls.return_value
        client.get_data_model_roles_strict.return_value = self.sales_roles
        client.get_role_assignments_strict.return_value = []
        self.client.force_login(self.admin)
        url = reverse('powerbi_report:report_rls_save', args=[self.sales.pbirs_id])
        response = self.client.post(url, {'identity': 'e1', 'roles': [self.alger, self.oran]})
        self.assertRedirects(response, reverse('powerbi_report:report_permissions', args=[self.sales.pbirs_id]),
                             fetch_redirect_response=False)
        sent = client.set_role_assignments_strict.call_args[0][1]
        self.assertEqual([(a['GroupUserName'].split('\\')[-1], a['DataModelRoles']) for a in sent], [('e1', [self.alger, self.oran])])

    @patch('powerbi_report.views.PBIRSClient')
    def test_save_refuses_a_user_without_access_and_non_admins(self, client_cls):
        client = client_cls.return_value
        client.get_report_policies_strict.return_value = [{'GroupUserName': 'DOM\\someone-else'}]
        self.client.force_login(self.admin)
        url = reverse('powerbi_report:report_rls_save', args=[self.sales.pbirs_id])
        self.client.post(url, {'identity': 'e1', 'roles': [self.alger]})  # emp has no access to Ventes
        client.set_role_assignments_strict.assert_not_called()
        self.client.logout()
        self.client.force_login(self.emp)
        self.client.post(url, {'identity': 'e1', 'roles': [self.alger]})
        client.set_role_assignments_strict.assert_not_called()
        self.assertEqual(self.client.get(url).status_code in (403, 405), True)
