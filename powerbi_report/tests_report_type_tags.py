"""The report list shows each report's type as a tag, next to "Consolidé", so admins need no pop-up to know it."""
import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from powerbi_report.models import ReportRef

User = get_user_model()


class ReportTypeTagTests(TestCase):
    def setUp(self):
        admin = User.objects.create_superuser(username="boss", password="x", ad2000="boss1")
        self.client.force_login(admin)
        ReportRef.objects.create(pbirs_id="t1", name="Rapport Achat", path="/BI/Achat", report_type="dashboard", is_consolide=True)
        ReportRef.objects.create(pbirs_id="t2", name="Rapport Alerte", path="/BI/Alerte", report_type="anomalie")
        ReportRef.objects.create(pbirs_id="t3", name="Rapport Livre", path="/BI/Livre", report_type="bibliotheque")
        ReportRef.objects.create(pbirs_id="t4", name="Rapport Sans Type", path="/BI/Sans")
        for target in ("requests.get", "requests.post", "requests.request"):
            patcher = patch(target, side_effect=AssertionError("PBIRS must not be contacted"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def row(self, name):
        html = self.client.get(reverse("powerbi_report:report_list")).content.decode()
        rows = [r for r in re.findall(r'<tr class="report-row.*?</tr>', html, re.S) if name in r]
        self.assertEqual(len(rows), 1, name)
        return rows[0]

    def test_a_dashboard_shows_its_tag_next_to_consolide(self):
        row = self.row("Rapport Achat")
        self.assertIn("Consolidé", row)
        self.assertIn("Dashboard", row)
        self.assertLess(row.index("Consolidé"), row.index("Dashboard"))

    def test_each_type_gets_its_own_tag(self):
        self.assertIn("Anomalie", self.row("Rapport Alerte"))
        self.assertIn("Bibliothèque", self.row("Rapport Livre"))
        self.assertNotIn("Dashboard", self.row("Rapport Alerte"))

    def test_a_report_without_a_type_or_consolide_has_no_tag(self):
        row = self.row("Rapport Sans Type")
        for label in ("Dashboard", "Anomalie", "Bibliothèque", "Consolidé"):
            self.assertNotIn(label, row)
