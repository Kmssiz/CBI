"""The command that uploads the Odoo-style direction icons as real logos."""
import io
import tempfile
from pathlib import Path
from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from PIL import Image

from powerbi_report.management.commands.seed_direction_icons import ICON_DIR, icon_for
from powerbi_report.models import MetadataOption

NAMES = {
    'Achat et Approvisionnement': 'achat',
    'Commerce': 'commerce',
    'Finance et Comptabilité': 'finance',
    'Générale': 'generale',
    'Logistique': 'logistique',
    'Maintenance': 'maintenance',
    'Production': 'production',
    'Ressource Humaine': 'rh',
    'Technique': 'technique',
}


class IconMatchingTests(SimpleTestCase):
    def test_each_direction_name_gets_its_own_icon(self):
        for name, icon in NAMES.items():
            self.assertEqual(icon_for(name), icon, name)
        self.assertEqual(len(set(NAMES.values())), len(NAMES))

    def test_case_and_accents_do_not_matter_and_unknown_names_match_nothing(self):
        self.assertEqual(icon_for(' FINANCE ET COMPTABILITE '), 'finance')
        self.assertEqual(icon_for('Ressources Humaines'), 'rh')
        self.assertIsNone(icon_for('Marketing'))
        self.assertIsNone(icon_for(''))

    def test_the_png_files_are_valid_uploads(self):
        for icon in NAMES.values():
            data = (ICON_DIR / f'{icon}.png').read_bytes()
            self.assertLess(len(data), 2 * 1024 * 1024, icon)           # the admin upload limit
            image = Image.open(io.BytesIO(data))
            self.assertEqual((image.format, image.size), ('PNG', (512, 512)), icon)


class SeedCommandTests(TestCase):
    def setUp(self):
        media = self.enterContext(override_settings(MEDIA_ROOT=tempfile.mkdtemp()))
        self.media = Path(tempfile.gettempdir())
        for name in NAMES:
            MetadataOption.objects.create(option_type='direction', name=name)
        self.other = MetadataOption.objects.create(option_type='direction', name='Marketing')
        self.pole = MetadataOption.objects.create(option_type='pole', name='Production')

    def run_command(self, *args):
        out = StringIO()
        call_command('seed_direction_icons', *args, stdout=out)
        return out.getvalue()

    def test_dry_run_changes_nothing(self):
        text = self.run_command('--dry-run')
        self.assertIn('would be uploaded', text)
        self.assertFalse(MetadataOption.objects.exclude(logo='').exclude(logo__isnull=True).exists())

    def test_it_uploads_each_icon_like_an_admin_upload(self):
        self.run_command()
        for name, icon in NAMES.items():
            option = MetadataOption.objects.get(option_type='direction', name=name)
            self.assertTrue(option.logo, name)
            self.assertRegex(option.logo.name, r'^metadata_logos/direction-[0-9a-f]+\.png$')   # same naming as the admin form
            self.assertEqual(option.logo.read(), (ICON_DIR / f'{icon}.png').read_bytes())
            option.logo.close()

    def test_unmatched_directions_and_other_sections_are_left_alone(self):
        self.run_command()
        for untouched in (self.other, self.pole):
            untouched.refresh_from_db()
            self.assertFalse(untouched.logo)

    def test_an_uploaded_logo_is_kept_unless_overwrite_is_asked(self):
        commerce = MetadataOption.objects.get(name='Commerce')
        self.run_command()
        commerce.refresh_from_db()
        first = commerce.logo.name
        self.assertIn('already has one', self.run_command())
        commerce.refresh_from_db()
        self.assertEqual(commerce.logo.name, first)
        self.run_command('--overwrite')
        commerce.refresh_from_db()
        self.assertNotEqual(commerce.logo.name, first)   # a new file name: the mobile cache is refreshed too
        self.assertFalse(commerce.logo.storage.exists(first))
