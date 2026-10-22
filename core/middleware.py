"""Server-side page view counting.

Every successful HTML page load on the public site is recorded as a PageView
row with a privacy-friendly visitor key: a hash of the visitor's IP address
and browser that rotates daily, so nobody is tracked across days and no
cookie or third-party script is involved. Obvious bots, the Django admin,
static files and JSON/modal endpoints are skipped.
"""
import hashlib
import logging
import re

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

SKIP_PREFIXES = ('/admin/', '/static/', '/media/')
SKIP_PATHS = {'/score/sw.js', '/score/manifest.json', '/favicon.ico'}

# Crawlers, monitors and command-line tools. Kept deliberately broad; a
# missed bot inflates a number, a missed human loses one, and neither matters
# much at this scale.
BOT_RE = re.compile(
    r'bot|crawl|spider|slurp|fetch|monitor|curl|wget|python-requests|httpx|'
    r'headless|lighthouse|pingdom|uptime|facebookexternalhit|preview',
    re.IGNORECASE,
)


def client_ip(request):
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '') or ''


def visitor_key(request, day):
    """A stable-for-today, unlinkable-tomorrow id for the visitor."""
    raw = '|'.join([
        settings.SECRET_KEY,
        day.isoformat(),
        client_ip(request),
        request.META.get('HTTP_USER_AGENT', '')[:300],
    ])
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:40]


def should_record(request, response):
    if not getattr(settings, 'PAGEVIEW_TRACKING', True):
        return False
    if request.method != 'GET' or response.status_code != 200:
        return False
    if request.path.startswith(SKIP_PREFIXES) or request.path in SKIP_PATHS:
        return False
    content_type = response.get('Content-Type', '')
    if not content_type.startswith('text/html'):
        return False
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return False
    if BOT_RE.search(request.META.get('HTTP_USER_AGENT', '')):
        return False
    return True


class PageViewMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            if should_record(request, response):
                self.record(request)
        except Exception:  # never let counting break a page
            logger.exception('page view not recorded')
        return response

    @staticmethod
    def record(request):
        from core.models import PageView
        from core.views import get_active_league

        now = timezone.now()
        day = timezone.localtime(now).date()
        user = getattr(request, 'user', None)
        PageView.objects.create(
            league=get_active_league(request),
            path=request.path[:255],
            visitor_key=visitor_key(request, day),
            day=day,
            is_staff=bool(user and user.is_authenticated and user.is_staff),
            created_at=now,
        )
