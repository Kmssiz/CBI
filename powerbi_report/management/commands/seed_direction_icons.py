"""
Uploads the Odoo-style icons of the directions as their logo, exactly as an admin would do it in
Administration > Serveurs (same field, same storage, so the web portal and the mobile app both show them).

    python manage.py seed_direction_icons --dry-run     # shows what would happen
    python manage.py seed_direction_icons               # directions without a logo get one
    python manage.py seed_direction_icons --overwrite   # also replaces logos that were already uploaded

A direction is matched to its icon by name (case and accents ignored). Directions that match no icon
are left alone. The PNG files live in powerbi_report/data/direction_icons/ (see tools/build_direction_icons.py).
"""
import unicodedata
from pathlib import Path

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand

from powerbi_report.models import MetadataOption

ICON_DIR = Path(__file__).resolve().parent.parent.parent / 'data' / 'direction_icons'

# (words found in the normalised direction name, icon file). First match wins.
KEYWORDS = (
    (('achat', 'approvision'), 'achat'),
    (('commerc', 'vente'), 'commerce'),
    (('financ', 'comptab'), 'finance'),
    (('general',), 'generale'),
    (('logisti',), 'logistique'),
    (('maintenan',), 'maintenance'),
    (('product',), 'production'),
    (('ressource', 'humain', ' rh', 'rh '), 'rh'),
    (('techni',), 'technique'),
    (('qualit',), 'qualite'),
)


def _normalise(name: str) -> str:
    text = unicodedata.normalize('NFKD', name or '')
    return f" {''.join(ch for ch in text if not unicodedata.combining(ch)).lower().strip()} "


def icon_for(name: str):
    """File name (without extension) of the icon of a direction, or None."""
    text = _normalise(name)
    for words, icon in KEYWORDS:
        if any(word in text for word in words):
            return icon
    return None


class Command(BaseCommand):
    help = "Upload the built-in direction icons as the logo of each direction (skips directions that already have one)."

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help="Only show what would be done.")
        parser.add_argument('--overwrite', action='store_true', help="Also replace logos that were already uploaded.")

    def handle(self, *args, **options):
        dry_run, overwrite = options['dry_run'], options['overwrite']
        done = skipped = unmatched = 0

        for option in MetadataOption.objects.filter(option_type='direction').order_by('name'):
            icon = icon_for(option.name)
            if icon is None:
                unmatched += 1
                self.stdout.write(f"  no icon for      {option.name}")
                continue
            if option.logo and not overwrite:
                skipped += 1
                self.stdout.write(f"  already has one  {option.name}")
                continue

            data = (ICON_DIR / f'{icon}.png').read_bytes()
            self.stdout.write(f"  {'would set' if dry_run else 'set'} {icon}.png  ->  {option.name}")
            if not dry_run:
                if option.logo:
                    option.logo.delete(save=False)   # the replaced file must not stay behind in the media folder
                option.logo.save(f'{icon}.png', ContentFile(data), save=True)
            done += 1

        verb = 'would be uploaded' if dry_run else 'uploaded'
        self.stdout.write(self.style.SUCCESS(
            f"{done} logo(s) {verb}, {skipped} kept (already had one), {unmatched} without a matching icon."
        ))
