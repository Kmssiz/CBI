"""Power BI mobile (phone) layouts for the app.

PBIRS only shows a report's mobile layout in Microsoft's Power BI Mobile app;
its web page always renders the desktop layout. The layout is still in the
report file: in ``Report/Layout`` every visual container's ``config.layouts``
holds ``id 0`` (desktop position) and ``id 1`` (phone position), and
``Report/MobileState`` holds phone-only formatting (``mobileObjects``).

This module extracts that phone layout from the .pbix (downloaded with the
PBIRS service account) into a compact map the app injects into the report
page, where a script rewrites the layout PBIRS sends to its renderer (see
``PBI-app/new_app/assets/js/pbi_mobile_layout.js``). Pages without a phone
layout are left out, so they keep the desktop view.
"""
import io
import json
import logging
import zipfile
from datetime import timedelta

import requests
from django.conf import settings
from django.utils import timezone
from requests_ntlm import HttpNtlmAuth

from powerbi_report.models import ReportRef

from .models import ReportMobileLayout

logger = logging.getLogger('powerbi_report')

FORMAT_VERSION = 1
PHONE_LAYOUT_ID = 1
MIN_PHONE_CANVAS_WIDTH = 320
MAX_PBIX_BYTES = 300 * 1024 * 1024
RECHECK_AFTER = timedelta(minutes=15)
REQUEST_TIMEOUT = 60


class MobileLayoutUnavailable(Exception):
    """The .pbix could not be fetched or read (the app keeps the desktop view)."""


def _json_part(archive: zipfile.ZipFile, name: str):
    """Report parts are UTF-16 LE JSON (UTF-8 in some exports)."""
    try:
        raw = archive.read(name)
    except KeyError:
        return None
    for encoding in ('utf-16-le', 'utf-8-sig'):
        try:
            return json.loads(raw.decode(encoding).lstrip('﻿'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    raise MobileLayoutUnavailable(f'{name} is not valid JSON')


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _mobile_objects(mobile_state: dict | None) -> dict[str, dict[str, dict]]:
    """{section name: {visual name: mobileObjects}} from Report/MobileState."""
    if not mobile_state:
        return {}
    state = _as_dict(mobile_state.get('explorationState'))
    result: dict[str, dict[str, dict]] = {}
    for section_name, section in (state.get('sections') or {}).items():
        visuals = {}
        for visual_name, container in (section.get('visualContainers') or {}).items():
            objects = (container.get('singleVisual') or {}).get('mobileObjects')
            if isinstance(objects, dict):
                # Drop empty entries such as {"items": [{"properties": {}}]}.
                cleaned = {
                    key: [entry for entry in entries if isinstance(entry, dict) and entry.get('properties')]
                    for key, entries in objects.items() if isinstance(entries, list)
                }
                cleaned = {key: entries for key, entries in cleaned.items() if entries}
                if cleaned:
                    visuals[visual_name] = cleaned
        if visuals:
            result[section_name] = visuals
    return result


def extract_mobile_layout(pbix: bytes) -> dict:
    """Phone layout of every page that has one: {"version", "pages": {section name: page}}."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(pbix))
    except zipfile.BadZipFile as exc:
        raise MobileLayoutUnavailable('not a .pbix archive') from exc
    with archive:
        layout = _json_part(archive, 'Report/Layout')
        if layout is None:
            raise MobileLayoutUnavailable('Report/Layout missing (PBIR format is not supported yet)')
        phone_objects = _mobile_objects(_json_part(archive, 'Report/MobileState'))

    pages = {}
    for section in layout.get('sections') or []:
        section_name = section.get('name')
        visuals = {}
        for container in section.get('visualContainers') or []:
            config = _as_dict(container.get('config'))
            phone = next(
                (item.get('position') for item in config.get('layouts') or []
                 if item.get('id') == PHONE_LAYOUT_ID and isinstance(item.get('position'), dict)),
                None,
            )
            if not config.get('name') or not phone:
                continue
            visual = {key: phone.get(key, 0) for key in ('x', 'y', 'z', 'width', 'height')}
            objects = phone_objects.get(section_name, {}).get(config['name'])
            if objects:
                visual['objects'] = objects
            visuals[config['name']] = visual
        if not section_name or not visuals:
            continue
        pages[section_name] = {
            'display_name': section.get('displayName') or section_name,
            'width': max(MIN_PHONE_CANVAS_WIDTH, max(v['x'] + v['width'] for v in visuals.values())),
            'height': max(v['y'] + v['height'] for v in visuals.values()),
            'visuals': visuals,
        }
    return {'version': FORMAT_VERSION, 'pages': pages}


# --- PBIRS access ------------------------------------------------------------

def _service_auth() -> HttpNtlmAuth:
    username, password = settings.LDAP_SERVICE_USERNAME, settings.LDAP_SERVICE_PASSWORD
    if not (username and password):
        raise MobileLayoutUnavailable('PBIRS service account not configured')
    return HttpNtlmAuth(f'{settings.LDAP_DOMAIN}\\{username}', password)


def _server_url(report: ReportRef) -> str:
    from powerbi_report.services.pbirs_servers import get_primary_pbirs_server_url
    url = (report.server_url or get_primary_pbirs_server_url() or '').rstrip('/')
    if not url:
        raise MobileLayoutUnavailable('no PBIRS server configured')
    return url


def _pbirs_modified(report: ReportRef, auth: HttpNtlmAuth) -> str:
    url = f"{_server_url(report)}/Reports/api/v2.0/PowerBIReports({report.pbirs_id})"
    try:
        response = requests.get(url, auth=auth, timeout=REQUEST_TIMEOUT, headers={'Accept': 'application/json'})
        response.raise_for_status()
        return str(response.json().get('ModifiedDate') or '')
    except (requests.RequestException, ValueError) as exc:
        raise MobileLayoutUnavailable(f'PBIRS metadata: {exc}') from exc


def _download_pbix(report: ReportRef, auth: HttpNtlmAuth) -> bytes:
    url = f"{_server_url(report)}/Reports/api/v2.0/PowerBIReports({report.pbirs_id})/Content/$value"
    try:
        with requests.get(url, auth=auth, timeout=REQUEST_TIMEOUT, stream=True) as response:
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                size += len(chunk)
                if size > MAX_PBIX_BYTES:
                    raise MobileLayoutUnavailable('report file too large')
                chunks.append(chunk)
            return b''.join(chunks)
    except requests.RequestException as exc:
        raise MobileLayoutUnavailable(f'PBIRS download: {exc}') from exc


def mobile_layout_for(report: ReportRef) -> dict:
    """Cached phone layout of ``report``; re-extracted when the report changes on PBIRS."""
    cached = ReportMobileLayout.objects.filter(report=report).first()
    now = timezone.now()
    if cached and cached.format_version == FORMAT_VERSION and now - cached.checked_at < RECHECK_AFTER:
        return cached.data

    auth = _service_auth()
    try:
        modified = _pbirs_modified(report, auth)
        if cached and cached.format_version == FORMAT_VERSION and modified and modified == cached.source_modified:
            cached.checked_at = now
            cached.save(update_fields=['checked_at'])
            return cached.data
        data = extract_mobile_layout(_download_pbix(report, auth))
    except MobileLayoutUnavailable:
        if cached:  # PBIRS unreachable: serve the last known layout
            return cached.data
        raise
    ReportMobileLayout.objects.update_or_create(report=report, defaults={
        'data': data, 'source_modified': modified, 'checked_at': now, 'format_version': FORMAT_VERSION,
    })
    logger.info('Mobile layout extracted for report %s: %s page(s)', report.pk, len(data['pages']))
    return data
