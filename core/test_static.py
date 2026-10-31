import re
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.templatetags.static import static
from django.test import SimpleTestCase, override_settings

MANIFEST_STORAGES = {
    **settings.STORAGES,
    'staticfiles': {
        'BACKEND': 'django.contrib.staticfiles.storage.ManifestStaticFilesStorage',
    },
}

STATIC_TAG = re.compile(r"""{%\s*static\s+['"]([^'"]+)['"]\s*%}""")


def _template_static_paths():
    paths = set()
    for template in Path(settings.BASE_DIR).glob('*/templates/**/*.html'):
        paths.update(STATIC_TAG.findall(template.read_text(encoding='utf-8')))
    return paths


class ManifestStaticFilesTests(SimpleTestCase):
    """Production serves hashed static names from collectstatic's manifest. The
    test runner uses plain storage, so check the manifest build here: a broken
    url() in a CSS file or a {% static %} path that doesn't exist would
    otherwise only show up on deploy (as a failed collectstatic or a 500)."""

    def test_collectstatic_hashes_every_template_reference(self):
        paths = _template_static_paths()
        self.assertIn('core/js/team_full_schedule_modal.js', paths)

        with tempfile.TemporaryDirectory() as static_root, override_settings(
            STATIC_ROOT=static_root, STORAGES=MANIFEST_STORAGES, DEBUG=False,
        ):
            call_command('collectstatic', interactive=False, verbosity=0)
            for path in sorted(paths):
                with self.subTest(path=path):
                    url = static(path)
                    stem, ext = path.rsplit('.', 1)
                    self.assertRegex(url, rf'^/static/{re.escape(stem)}\.[0-9a-f]{{12}}\.{ext}$')
