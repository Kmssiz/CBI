from django.test import TestCase, RequestFactory
from django.contrib.auth import get_user_model
from django.urls import reverse
from unittest.mock import patch, Mock
import json
from .views import add_refresh_plan

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
