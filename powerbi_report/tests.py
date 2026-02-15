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
        self.user = User.objects.create_user(username='testuser', password='password')
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
