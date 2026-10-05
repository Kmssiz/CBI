import io
import json
import zipfile
from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests as http
from django.test import TestCase, override_settings
from django.utils import timezone

from mobile.mobile_layout import MobileLayoutUnavailable, extract_mobile_layout
from mobile.models import ReportMobileLayout
from mobile.tests import MobileTestCase, url


def make_pbix(sections, mobile_state=None, encoding='utf-16-le'):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('Report/Layout', json.dumps({'sections': sections}).encode(encoding))
        if mobile_state is not None:
            state = {'objectId': '', 'type': 4, 'explorationState': json.dumps(mobile_state)}
            archive.writestr('Report/MobileState', json.dumps(state).encode(encoding))
    return buffer.getvalue()


def pos(x, y, w, h, z=0):
    return {'x': x, 'y': y, 'z': z, 'width': w, 'height': h}


def visual(name, desktop, phone=None, parent=None):
    layouts = [{'id': 0, 'position': desktop}] + ([{'id': 1, 'position': phone}] if phone else [])
    config = {'name': name, 'layouts': layouts, 'singleVisual': {'visualType': 'card', 'objects': {}}}
    if parent:
        config['parentGroupName'] = parent
    return {'x': desktop['x'], 'y': desktop['y'], 'config': json.dumps(config)}


FONT_30 = {'labels': [{'properties': {'fontSize': {'expr': {'Literal': {'Value': '30D'}}}}}]}
SAMPLE_SECTIONS = [
    {'name': 'ReportSection', 'displayName': 'CA GLOBAL', 'width': 1330, 'height': 720, 'visualContainers': [
        visual('group1', pos(0, 0, 400, 200)),
        visual('card1', pos(10, 10, 200, 100), pos(10, 55, 144, 100, 3000), parent='group1'),
        visual('table1', pos(500, 0, 800, 600), pos(0, 500, 324, 420, 16000)),
        visual('desktopOnly', pos(0, 600, 100, 100)),
    ]},
    {'name': 'ReportSection2', 'displayName': 'Objectif par famille', 'width': 1330, 'height': 720,
     'visualContainers': [visual('chart', pos(0, 0, 600, 400))]},
]
SAMPLE_STATE = {'sections': {'ReportSection': {'visualContainers': {
    'card1': {'singleVisual': {'mobileObjects': {**FONT_30, 'categoryLabels': []}}},
    'table1': {'singleVisual': {'mobileObjects': {'items': [{'properties': {}}]}}},
}}}}


class MobileLayoutExtractionTests(TestCase):
    def test_extracts_phone_positions_and_formatting_per_page(self):
        data = extract_mobile_layout(make_pbix(SAMPLE_SECTIONS, SAMPLE_STATE))
        self.assertEqual(list(data['pages']), ['ReportSection'])  # page without phone layout omitted
        page = data['pages']['ReportSection']
        self.assertEqual((page['display_name'], page['width'], page['height']), ('CA GLOBAL', 324, 920))
        self.assertEqual(set(page['visuals']), {'card1', 'table1'})
        self.assertEqual(page['visuals']['card1'],
                         {'x': 10, 'y': 55, 'z': 3000, 'width': 144, 'height': 100, 'objects': FONT_30})
        self.assertNotIn('objects', page['visuals']['table1'])  # empty phone formatting dropped

    def test_utf8_parts_and_missing_mobile_state(self):
        data = extract_mobile_layout(make_pbix(SAMPLE_SECTIONS, encoding='utf-8'))
        self.assertNotIn('objects', data['pages']['ReportSection']['visuals']['card1'])

    def test_invalid_files(self):
        with self.assertRaises(MobileLayoutUnavailable):
            extract_mobile_layout(b'not a zip')
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('Report/definition/report.json', '{}')  # PBIR format
        with self.assertRaises(MobileLayoutUnavailable):
            extract_mobile_layout(buffer.getvalue())


@override_settings(LDAP_SERVICE_USERNAME='svc', LDAP_SERVICE_PASSWORD='secret', LDAP_DOMAIN='GSH')
class MobileLayoutEndpointTests(MobileTestCase):
    def setUp(self):
        super().setUp()
        self.report_ref = self.report('Chiffre Puma', is_consolide=True)
        self.forbidden = self.report('Secret', grant=False, is_consolide=True)
        self.pbix = make_pbix(SAMPLE_SECTIONS, SAMPLE_STATE)

    def fake_pbirs(self, modified='2026-08-02T11:41:00Z', fail=False):
        calls = {'metadata': 0, 'download': 0}

        def get(request_url, **kwargs):
            if fail:
                raise http.ConnectionError('PBIRS down')
            response = MagicMock()
            response.raise_for_status.return_value = None
            response.__enter__.return_value = response
            if request_url.endswith('/Content/$value'):
                calls['download'] += 1
                response.iter_content.return_value = [self.pbix]
            else:
                calls['metadata'] += 1
                response.json.return_value = {'ModifiedDate': modified}
            return response
        return get, calls

    def layout(self, report=None):
        return self.client.get(url('report_mobile_layout', (report or self.report_ref).pk), **self.auth)

    def expire_cache(self):
        ReportMobileLayout.objects.update(checked_at=timezone.now() - timedelta(hours=1))

    def test_serves_extracted_layout_and_caches_it(self):
        get, calls = self.fake_pbirs()
        with patch('mobile.mobile_layout.requests.get', side_effect=get) as mocked:
            body = self.layout().json()
            self.assertTrue(body['available'])
            self.assertEqual(body['pages']['ReportSection']['visuals']['table1']['width'], 324)
            self.assertTrue(mocked.call_args_list[-1].args[0].startswith(
                'http://10.20.10.63/Reports/api/v2.0/PowerBIReports(id-Chiffre Puma)/Content'))
            self.layout()  # within 15 min: served from the database, PBIRS not called
        self.assertEqual(calls, {'metadata': 1, 'download': 1})
        catalog = self.client.get(url('catalog'), **self.auth).json()
        self.assertTrue(catalog['reports'][str(self.report_ref.pk)]['has_mobile_layout'])

    def test_reextracts_only_when_the_report_changed(self):
        get, calls = self.fake_pbirs()
        with patch('mobile.mobile_layout.requests.get', side_effect=get):
            self.layout()
            self.expire_cache()
            self.layout()  # same ModifiedDate: metadata checked, no new download
        self.assertEqual(calls, {'metadata': 2, 'download': 1})
        self.expire_cache()
        changed, changed_calls = self.fake_pbirs(modified='2026-09-01T08:00:00Z')
        with patch('mobile.mobile_layout.requests.get', side_effect=changed):
            self.layout()
        self.assertEqual(changed_calls, {'metadata': 1, 'download': 1})

    def test_pbirs_down_serves_last_layout_or_reports_unavailable(self):
        down, _ = self.fake_pbirs(fail=True)
        with patch('mobile.mobile_layout.requests.get', side_effect=down):
            self.assertEqual(self.layout().json(), {'available': False, 'pages': {}})
        get, _ = self.fake_pbirs()
        with patch('mobile.mobile_layout.requests.get', side_effect=get):
            self.layout()
        self.expire_cache()
        with patch('mobile.mobile_layout.requests.get', side_effect=down):
            self.assertTrue(self.layout().json()['available'])

    def test_permission_and_missing_service_account(self):
        self.assertEqual(self.layout(self.forbidden).status_code, 404)
        with override_settings(LDAP_SERVICE_PASSWORD=''):
            self.assertFalse(self.layout().json()['available'])
