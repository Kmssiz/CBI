"""Server side of the interactive product tour.

The tours themselves (steps, texts) live in ``static/assets/js/cbi-tours.js``. The server only
remembers, per user, which tour was seen and at which version, so a tour is not shown again on
another browser or machine. Bumping a tour's ``version`` in the script shows it once more to
everyone, which is how a page that gained features announces them.
"""
import json
import logging
import re
from typing import Optional

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_POST

logger = logging.getLogger('users')

TOUR_KEY_RE = re.compile(r'^[a-z0-9_]{1,40}$')
MAX_TOURS = 40
MAX_VERSION = 1000
RESET_ALL = 'all'


def _read_json(request: HttpRequest) -> Optional[dict]:
    try:
        data = json.loads(request.body or b'{}')
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


@login_required
@require_POST
def tour_seen(request: HttpRequest) -> JsonResponse:
    """Remember that the user has seen (finished or dismissed) ``tour`` at ``version``."""
    data = _read_json(request)
    if data is None:
        return JsonResponse({'ok': False, 'error': 'invalid json'}, status=400)

    tour = data.get('tour')
    version = data.get('version')
    if not isinstance(tour, str) or not TOUR_KEY_RE.match(tour):
        return JsonResponse({'ok': False, 'error': 'invalid tour'}, status=400)
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= MAX_VERSION:
        return JsonResponse({'ok': False, 'error': 'invalid version'}, status=400)

    user = request.user
    seen = dict(user.tours_seen or {})
    if tour not in seen and len(seen) >= MAX_TOURS:
        return JsonResponse({'ok': False, 'error': 'too many tours'}, status=400)
    if seen.get(tour, 0) < version:
        seen[tour] = version
        user.tours_seen = seen
        user.save(update_fields=['tours_seen'])
    return JsonResponse({'ok': True, 'seen': seen})


@login_required
@require_POST
def tour_reset(request: HttpRequest) -> JsonResponse:
    """Forget one tour, or every tour (``tour = "all"``), so it plays again."""
    data = _read_json(request)
    if data is None:
        return JsonResponse({'ok': False, 'error': 'invalid json'}, status=400)

    tour = data.get('tour', RESET_ALL)
    if tour != RESET_ALL and not (isinstance(tour, str) and TOUR_KEY_RE.match(tour)):
        return JsonResponse({'ok': False, 'error': 'invalid tour'}, status=400)

    user = request.user
    seen = dict(user.tours_seen or {})
    if tour == RESET_ALL:
        seen = {}
    else:
        seen.pop(tour, None)
    user.tours_seen = seen
    user.save(update_fields=['tours_seen'])
    return JsonResponse({'ok': True, 'seen': seen})
