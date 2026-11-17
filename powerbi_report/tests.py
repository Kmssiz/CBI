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


class CustomFolderNavigationTests(TestCase):
    """Direction -> reports, and Pôle -> société -> direction -> reports."""

    def setUp(self):
        from powerbi_report.models import ReportRef, MetadataOption
        self.user = User.objects.create_user(
            username='nav_user', password='pw', ad2000='NAV001', status='Active',
            can_view_direction=True, can_view_pole=True,
        )
        mk = lambda kind, name: MetadataOption.objects.create(option_type=kind, name=name)
        self.pole, self.other_pole = mk('pole', 'Production'), mk('pole', 'Services')
        self.societe = mk('societe', 'Alpha')
        self.commerce, self.finance = mk('direction', 'Commerce'), mk('direction', 'Finance')

        def report(pbirs_id, name, poles=(), societes=(), directions=()):
            r = ReportRef.objects.create(pbirs_id=pbirs_id, name=name, path=f'/CBI/{name}')
            r.poles.set(poles)
            r.societes.set(societes)
            r.directions.set(directions)
            return r

        self.in_commerce = report('r1', 'Ventes', [self.pole], [self.societe], [self.commerce])
        self.in_finance = report('r2', 'Marges', [self.pole], [self.societe], [self.finance])
        self.no_direction = report('r3', 'Synthèse', [self.pole], [self.societe])
        self.no_societe = report('r4', 'Pôle seul', [self.pole])
        self.elsewhere = report('r5', 'Autre pôle', [self.other_pole], [self.societe], [self.commerce])
        self.client.force_login(self.user)
        patcher = patch('powerbi_report.views.get_visible_report_ids', return_value=['r1', 'r2', 'r3', 'r4', 'r5'])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _folder(self, view, folder, query=''):
        return self.client.get(reverse('powerbi_report:custom_folder_detail', args=[view, folder.id]) + query)

    def test_direction_goes_straight_to_the_reports(self):
        response = self._folder('direction', self.commerce)
        self.assertEqual(response.context['subfolders'], [])
        self.assertEqual({r['name'] for r in response.context['reports']}, {'Ventes', 'Autre pôle'})

    def test_pole_lists_its_societes_and_reports_without_societe(self):
        response = self._folder('pole', self.pole)
        self.assertEqual([f.name for f in response.context['subfolders']], ['Alpha'])
        self.assertEqual([r['name'] for r in response.context['reports']], ['Pôle seul'])
        self.assertEqual(response.context['subfolders'][0].nav_query, f'?pole={self.pole.id}')

    def test_societe_inside_a_pole_lists_directions_not_reports(self):
        response = self._folder('pole', self.societe, f'?pole={self.pole.id}')
        self.assertEqual([f.name for f in response.context['subfolders']], ['Commerce', 'Finance'])
        # Only the reports that have no direction stay at this level; other pôles' reports never show.
        self.assertEqual([r['name'] for r in response.context['reports']], ['Synthèse'])
        self.assertEqual(response.context['back_url'], reverse('powerbi_report:custom_folder_detail', args=['pole', self.pole.id]))

    def test_direction_inside_pole_and_societe_lists_the_reports(self):
        query = f'?pole={self.pole.id}&societe={self.societe.id}'
        response = self._folder('pole', self.commerce, query)
        self.assertEqual([r['name'] for r in response.context['reports']], ['Ventes'])
        self.assertEqual(response.context['subfolders'], [])
        self.assertEqual([f.name for f in response.context['breadcrumbs']], ['Production', 'Alpha', 'Commerce'])
        self.assertEqual(response.context['report_query'], query)
        self.assertEqual(
            response.context['back_url'],
            reverse('powerbi_report:custom_folder_detail', args=['pole', self.societe.id]) + f'?pole={self.pole.id}',
        )
        self.assertContains(response, 'Ventes')

    def test_navigation_params_of_the_wrong_type_are_ignored(self):
        # A direction id passed as the pôle must not be trusted.
        response = self._folder('pole', self.societe, f'?pole={self.commerce.id}')
        self.assertEqual(response.context['breadcrumbs'][0].name, 'Alpha')
        self.assertEqual(response.context['subfolders'], [])

    def test_embedded_report_page_shows_the_pole_societe_direction_path(self):
        url = reverse('powerbi_report:embed_custom_report', args=['pole', self.commerce.id, self.in_commerce.id])
        with patch('powerbi_report.views._user_can_access_report', return_value=True):
            response = self.client.get(url + f'?pole={self.pole.id}&societe={self.societe.id}')
        self.assertEqual([f.name for f in response.context['breadcrumbs']], ['Production', 'Alpha', 'Commerce'])


class ReportListTableTests(TestCase):
    """The report list: server shown by name, and the data of the details modal."""

    def setUp(self):
        from powerbi_report.models import MetadataOption, PBIRSServer, ReportRef
        self.admin = User.objects.create_superuser(username='listadmin', password='password')
        self.client.force_login(self.admin)
        PBIRSServer.objects.create(
            name='PBIRS Production', base_url='https://bi.example.com/',
        )
        pole = MetadataOption.objects.create(option_type='pole', name='Service')
        self.societes = [
            MetadataOption.objects.create(option_type='societe', name=n, parent=pole)
            for n in ('ALPHA', 'BETA', 'GAMMA', 'DELTA', 'EPSILON')
        ]
        self.direction = MetadataOption.objects.create(option_type='direction', name='Commerce')
        self.ref = ReportRef.objects.create(
            pbirs_id='r-1', name='Ventes', path='/CBI/Ventes', server_url='https://bi.example.com',
            description='Chiffre d\'affaires', report_type='dashboard',
        )
        self.ref.societes.set(self.societes)
        self.ref.poles.set([pole])
        self.ref.directions.set([self.direction])
        self.other = ReportRef.objects.create(
            pbirs_id='r-2', name='Autre', path='/CBI/Autre', server_url='http://10.0.0.5',
        )

    def _page(self):
        return self.client.get(reverse('powerbi_report:report_list'))

    def test_server_is_shown_by_name_and_falls_back_to_the_host(self):
        response = self._page()
        shown = {r['Name']: r['server_name'] for r in response.context['reports']}
        self.assertEqual(shown, {'Ventes': 'PBIRS Production', 'Autre': '10.0.0.5'})
        import re
        table = re.search(r'<table[^>]*id="reportTable".*?</table>', response.content.decode(), re.S).group(0)
        self.assertNotRegex(table, r'>\s*https?://')       # no address displayed as a cell's text
        self.assertIn('PBIRS Production', table)

    def test_modal_data_carries_the_full_lists(self):
        response = self._page()
        data = response.context['reports_modal']
        d = data['r-1']
        self.assertEqual(d['societes'], sorted(s.name for s in self.societes))
        self.assertEqual(d['poles'], ['Service'])
        self.assertEqual(d['directions'], ['Commerce'])
        self.assertEqual(d['server'], 'PBIRS Production')
        self.assertEqual(d['description'], "Chiffre d'affaires")
        self.assertIn('report-modal-data', response.content.decode())

    def test_row_is_clickable_and_actions_are_icon_buttons(self):
        html = self._page().content.decode()
        self.assertIn('data-report-id="r-1"', html)
        self.assertIn('data-act="detail"', html)                 # a link to the report page, not a second modal trigger
        for label in ('Détails', 'Accès', 'Ouvrir le rapport'):
            self.assertIn(f'title="{label}"', html)

    def test_table_has_only_the_wanted_columns(self):
        import re
        html = self._page().content.decode()
        headers = re.findall(r'<th class="[^"]*">([^<]+)</th>', html)
        self.assertEqual(headers, ['Nom', 'Serveur', 'Direction', 'Société', 'Actions'])


class PageTitleTests(TestCase):
    """Every page needs a real title: without one the browser tab shows the address."""

    PARTIALS_AND_MAILS = ('/email/', '/_')

    def _page_templates(self):
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        for path in root.rglob('*.html'):
            rel = path.relative_to(root).as_posix()
            if any(skip in '/' + rel for skip in self.PARTIALS_AND_MAILS):
                continue
            if rel.startswith(('venv/', 'staticfiles/', 'static/', 'node_modules/', 'tools/')) or '/node_modules/' in rel:
                continue
            if rel == 'templates/base.html':
                continue
            yield rel, path.read_text(encoding='utf-8'), re

    def test_every_page_template_defines_a_title(self):
        missing = []
        for rel, text, re in self._page_templates():
            if re.search(r"{%\s*extends\s", text):
                match = re.search(r"{%\s*block title\s*%}(.*?){%\s*endblock", text, re.S)
            elif '<html' in text.lower():
                match = re.search(r"<title>(.*?)</title>", text, re.S)
            else:
                continue                      # an include, not a page
            if not match or not match.group(1).strip():
                missing.append(rel)
        self.assertEqual(missing, [], 'pages without a title')

    def test_pages_have_no_english_leftovers_or_the_address_in_the_title(self):
        import re
        banned = ('Reports', 'Upload Report', 'Report Permissions')
        for rel, text, _ in self._page_templates():
            match = re.search(r"{%\s*block title\s*%}(.*?){%\s*endblock", text, re.S) or re.search(r"<title>(.*?)</title>", text, re.S)
            if match:
                self.assertNotIn(match.group(1).strip(), banned, rel)
                self.assertNotRegex(match.group(1), r'https?://|localhost', rel)

    def test_rendered_titles_carry_the_site_suffix(self):
        admin = User.objects.create_superuser(username='titleadmin', password='password')
        self.client.force_login(admin)
        for name in ('tickets:ticket_list', 'tickets:create_ticket', 'powerbi_report:report_list'):
            html = self.client.get(reverse(name)).content.decode()
            title = html.split('<title>')[1].split('</title>')[0]
            self.assertTrue(title.endswith(' | CBI') and len(title) > len(' | CBI'), (name, title))
        self.client.logout()
        login_title = self.client.get(reverse('login')).content.decode().split('<title>')[1].split('</title>')[0]
        self.assertEqual(login_title, 'Connexion | CBI')


class ButtonSpinnerAssetsTests(TestCase):
    """The in-button spinner is loaded on every page, including the login page."""

    def test_assets_exist_and_are_loaded_by_base_and_login(self):
        from django.contrib.staticfiles import finders
        self.assertTrue(finders.find('assets/js/cbi-loading.js'))
        self.assertTrue(finders.find('assets/css/cbi-loading.css'))
        admin = User.objects.create_superuser(username='spinadmin', password='password')
        self.client.force_login(admin)
        pages = {'app page': self.client.get(reverse('tickets:ticket_list'))}
        self.client.logout()
        pages['login page'] = self.client.get(reverse('login'))
        for label, response in pages.items():
            html = response.content.decode()
            self.assertIn('cbi-loading.js', html, label)
            self.assertIn('cbi-loading.css', html, label)

    def test_the_old_fullscreen_loader_is_gone(self):
        admin = User.objects.create_superuser(username='loaderadmin', password='password')
        self.client.force_login(admin)
        html = self.client.get(reverse('tickets:ticket_list')).content.decode()
        self.assertNotIn('globalLoadingOverlay', html)


class NavSectionTests(TestCase):
    """'Tous les rapports' keeps the user inside the section he came from, and the menu keeps its highlight."""

    SECTIONS = [('direction', 'Direction'), ('pole', 'Pôle'), ('consolide', 'Consolidé'),
                ('biblio', 'Bibliothèque'), ('module', 'Module'), ('anomalie', 'Anomalie')]

    def setUp(self):
        self.admin = User.objects.create_superuser(username='navadmin', password='password')
        self.client.force_login(self.admin)

    def _flat(self, query=''):
        return self.client.get(reverse('powerbi_report:report_list_flat') + query)

    @staticmethod
    def _classes(html, label, tag):
        import re
        match = re.search(r'class="([^"]*)">' + re.escape(label) + r'</' + tag + '>', html)
        assert match, f'{label!r} not found in the menu'
        return match.group(1)

    def test_every_section_keeps_its_own_highlight_on_tous_les_rapports(self):
        for key, label in self.SECTIONS:
            with self.subTest(key):
                response = self._flat(f'?context={key}')
                html = response.content.decode()
                self.assertEqual(response.context['nav_section'], key)
                self.assertIn('is-current', self._classes(html, label, 'a'))
                for other_key, other_label in self.SECTIONS:
                    if other_key != key:
                        self.assertNotIn('is-current', self._classes(html, other_label, 'a'))
                self.assertIn('is-current', self._classes(html, 'Tableau de bord', 'summary'))
                self.assertNotIn('is-current', self._classes(html, 'Admin', 'summary'))   # the highlight no longer jumps to Admin
                self.assertRegex(html, r'<details class="legacy-report-group"[^>]*\bopen\b')

    def test_the_flat_list_outside_any_section_still_belongs_to_the_admin_area(self):
        html = self._flat().content.decode()
        self.assertEqual(self._flat().context['nav_section'], '')
        self.assertIn('is-current', self._classes(html, 'Admin', 'summary'))
        self.assertNotIn('is-current', self._classes(html, 'Tableau de bord', 'summary'))

    def test_an_unknown_context_is_not_a_section(self):
        for context in ('list_flat', 'nonsense', ''):
            with self.subTest(context):
                self.assertEqual(self._flat(f'?context={context}').context['nav_section'], '')

    def test_the_section_pages_themselves_are_still_highlighted_by_their_path(self):
        for key, _ in self.SECTIONS:
            with self.subTest(key):
                response = self.client.get(reverse(f'powerbi_report:custom_{key}'))
                self.assertEqual(response.context['nav_section'], key)

    def test_title_header_and_back_button_stay_in_the_section(self):
        for key, label in self.SECTIONS:
            with self.subTest(key):
                response = self._flat(f'?context={key}')
                html = response.content.decode()
                section_url = reverse(f'powerbi_report:custom_{key}')
                self.assertContains(response, f'{label} · Tous les rapports')
                self.assertIn(f'<title>Tous les rapports · {label} | CBI</title>', html)
                self.assertEqual(response.context['nav_section_url'], section_url)
                self.assertIn(f'href="{section_url}" class="flex items-center gap-2 px-4 py-2 border', html)   # "Vue Hiérarchique"

    def test_the_report_page_opened_from_a_section_keeps_that_section(self):
        from django.test import RequestFactory
        from django.urls import resolve
        from powerbi_report.context_processors import nav_section
        request = RequestFactory().get('/powerbi/embed/CBI/Pole%20Production/rapport/', {'context': 'pole'})
        request.resolver_match = resolve(request.path)
        self.assertEqual(request.resolver_match.url_name, 'embed_report')
        self.assertEqual(nav_section(request)['nav_section'], 'pole')
        request = RequestFactory().get('/powerbi/embed/CBI/x/', {'context': 'list_flat'})
        request.resolver_match = resolve(request.path)
        self.assertEqual(nav_section(request), {'nav_section': ''})

    def test_the_context_is_kept_by_the_links_of_the_list(self):
        from powerbi_report.models import MetadataOption, ReportRef
        ref = ReportRef.objects.create(pbirs_id='nav-1', name='Ventes', path='/CBI/Ventes', report_type='dashboard')
        ref.directions.add(MetadataOption.objects.create(option_type='direction', name='Commerce'))
        html = self._flat('?context=direction').content.decode()
        self.assertIn('?context=direction', html)
        self.assertIn('Ventes', html)


class NavigationUiTests(TestCase):
    """Library by module, folder search + 'Tous les rapports', sidebar label, header controls."""

    def setUp(self):
        from powerbi_report.models import MetadataOption, ReportRef
        self.admin = User.objects.create_superuser(username='nav_admin', password='x')
        self.plain = User.objects.create_user(username='nav_user', password='x')
        self.mod_fin = MetadataOption.objects.create(option_type='module', name='Finance')
        self.dir_cpo = MetadataOption.objects.create(option_type='direction', name='CPO')
        self.lib = ReportRef.objects.create(pbirs_id='lib-1', name='Balance âgée', path='/BI/Balance', report_type='bibliotheque')
        self.lib.modules.add(self.mod_fin)
        self.lib.directions.add(self.dir_cpo)
        self.loose = ReportRef.objects.create(pbirs_id='lib-2', name='Rapport libre', path='/BI/Libre', report_type='bibliotheque')

    def test_the_library_is_grouped_by_module_not_by_direction(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse('powerbi_report:custom_biblio'))
        self.assertEqual(page.status_code, 200)
        self.assertEqual([f.name for f in page.context['subfolders']], ['Finance'])
        self.assertContains(page, 'Finance')
        self.assertNotContains(page, '>CPO<')

    def test_a_library_report_without_module_is_still_reachable_from_the_root(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse('powerbi_report:custom_biblio'))
        self.assertEqual([r['name'] for r in page.context['loose_reports']], ['Rapport libre'])
        self.assertContains(page, 'Rapport libre')
        self.assertContains(page, 'Sans module')

    def test_a_module_folder_lists_its_reports(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse('powerbi_report:custom_folder_detail', args=['biblio', self.mod_fin.id]))
        self.assertEqual([r['name'] for r in page.context['reports']], ['Balance âgée'])

    def test_folders_have_a_search_bar_and_the_all_reports_button_on_every_level(self):
        self.client.force_login(self.admin)
        flat = reverse('powerbi_report:report_list_flat') + '?context=biblio'
        root = self.client.get(reverse('powerbi_report:custom_biblio'))
        inside = self.client.get(reverse('powerbi_report:custom_folder_detail', args=['biblio', self.mod_fin.id]))
        for page in (root, inside):
            self.assertContains(page, 'id="folderSearch"')
            self.assertContains(page, 'Tous les rapports')
            self.assertContains(page, flat)
        self.assertContains(inside, 'data-search-card')

    def test_the_sidebar_calls_the_server_page_config(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse('powerbi_report:server_management_list'))
        self.assertContains(page, '>Config</a>')
        self.assertNotContains(page, '>Serveurs</a>')

    def test_one_theme_switch_in_the_dock_and_none_in_the_hidden_header(self):
        self.client.force_login(self.plain)
        page = self.client.get(reverse('landing'))
        body = page.content.decode()
        self.assertEqual(body.count('id="theme-toggle"'), 1)
        self.assertIn('role="switch"', body)
        self.assertNotIn('legacy-theme-slot', body)
        self.assertNotIn('theme-toggle-header', body)

    def test_the_help_button_is_for_regular_users_only(self):
        self.client.force_login(self.plain)
        self.assertContains(self.client.get(reverse('landing')), 'id="tour-launcher"')
        self.client.force_login(self.admin)
        self.assertNotContains(self.client.get(reverse('landing')), 'id="tour-launcher"')


class PerformanceFixesTests(TestCase):
    """The cheap, behaviour-neutral speed-ups: same answers, fewer queries and calls."""

    def test_the_leaf_filter_keeps_reports_and_drops_folder_like_rows(self):
        from powerbi_report.models import ReportRef
        from powerbi_report.views import _filter_reportref_to_leaf_items
        refs = [ReportRef.objects.create(pbirs_id=f'p-{i}', name=path or 'vide', path=path)
                for i, path in enumerate(['/CBI', '/CBI/A', '/CBI/A/B', '/CBI/C', '/CBI/Ab', '/', '', '/Z/'])]
        kept = sorted(r.path for r in _filter_reportref_to_leaf_items(refs))
        # '/CBI' and '/CBI/A' have children; '/CBI/Ab' is NOT a parent of anything ('/CBI/A' is not its prefix + '/')
        self.assertEqual(kept, ['/CBI/A/B', '/CBI/Ab', '/CBI/C', '/Z/'])

    def test_the_server_list_is_cached_and_a_change_clears_it(self):
        from powerbi_report.models import PBIRSServer
        from powerbi_report.services.pbirs_servers import get_primary_pbirs_server_url
        server = PBIRSServer.objects.create(name='A', base_url='http://one.example', is_active=True)
        self.assertEqual(get_primary_pbirs_server_url(), 'http://one.example')
        with self.assertNumQueries(0):
            self.assertEqual(get_primary_pbirs_server_url(), 'http://one.example')
        server.base_url = 'http://two.example'
        server.save()
        self.assertEqual(get_primary_pbirs_server_url(), 'http://two.example')
        server.delete()
        self.assertNotEqual(get_primary_pbirs_server_url(), 'http://two.example')  # back to the configured fallback

    def test_the_local_report_list_costs_the_same_number_of_queries_whatever_the_number_of_reports(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from powerbi_report.models import ReportRef
        from powerbi_report.views import get_local_reports_for_user
        admin = User.objects.create_superuser(username='perf_admin', password='x')

        def queries_for(count):
            ReportRef.objects.all().delete()
            for i in range(count):
                ReportRef.objects.create(pbirs_id=f'm-{i}', name=f'R{i}', path=f'/CBI/R{i}', modified_by=admin)
            get_local_reports_for_user(admin)            # warm the server-url cache: only the report queries are counted
            with CaptureQueriesContext(connection) as captured:
                reports = get_local_reports_for_user(admin)
            self.assertEqual(len(reports), count)
            return len(captured)

        self.assertEqual(queries_for(3), queries_for(15))   # the modifier used to cost one query per report

    @patch('powerbi_report.views.get_powerbi_reports')
    @patch('powerbi_report.views.get_group_members', return_value=[])
    @patch('powerbi_report.views.is_ldap_group', return_value=False)
    @patch('powerbi_report.views.get_report_permissions')
    @patch('powerbi_report.services.pbirs_client.PBIRSClient')
    def test_the_permissions_page_skips_the_catalogue_and_the_group_lookup_for_known_people(
            self, client_cls, policies, is_group, _members, catalogue):
        from powerbi_report.models import ReportRef
        from powerbi_report.services.pbirs_client import PBIRSClientError
        ReportRef.objects.create(pbirs_id='perm-1', name='Ventes', path='/CBI/Ventes')
        User.objects.create_user(username='known', password='x', ad2000='KNOWN1')
        policies.return_value = [{'GroupUserName': r'DOM\KNOWN1', 'Roles': []}, {'GroupUserName': r'DOM\SOME-GROUP', 'Roles': []}]
        client_cls.return_value.get_data_model_roles_strict.side_effect = PBIRSClientError('no rls here')
        admin = User.objects.create_superuser(username='perm_admin', password='x')
        self.client.force_login(admin)
        page = self.client.get(reverse('powerbi_report:report_permissions', args=['perm-1']))
        self.assertEqual(page.status_code, 200)
        catalogue.assert_not_called()                                   # the local row gave the name
        self.assertEqual([c.args[0] for c in is_group.call_args_list], ['SOME-GROUP'])   # not asked about KNOWN1
        self.assertContains(page, 'Ventes')

    @patch('powerbi_report.views.get_powerbi_report_info')
    def test_the_report_label_comes_from_the_local_row_without_calling_pbirs(self, pbirs_info):
        from powerbi_report.models import ReportRef
        from powerbi_report.views import _get_report_label
        ReportRef.objects.create(pbirs_id='lbl-1', name='Marges', path='/CBI/Marges')
        self.assertEqual(_get_report_label(Mock(), 'LBL-1'), {'name': 'Marges', 'path': '/CBI/Marges'})
        pbirs_info.assert_not_called()


class LoginKeepsAdGroupsTests(TestCase):
    def test_logging_in_does_not_erase_the_cached_ad_groups(self):
        user = User.objects.create_user(username='grp_user', password='x', ad2000='GRP1', ad_groups=['BI-Finance', 'BI-RH'])
        ldap_answer = {'username': 'grp_user', 'email': 'g@example.com', 'first_name': 'Gil', 'last_name': 'Roup',
                       'ad2000': 'GRP1', 'department': '', 'title': ''}   # the login lookup has no 'ad_groups'
        with patch('users.views.connexion_ad2000', return_value=ldap_answer), \
                patch('users.views.sync_user_permissions_on_login', create=True):
            self.client.post(reverse('login'), {'username': 'grp_user', 'password': 'x'})
        user.refresh_from_db()
        self.assertEqual(user.ad_groups, ['BI-Finance', 'BI-RH'])
