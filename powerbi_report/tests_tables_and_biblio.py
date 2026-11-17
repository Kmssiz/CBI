"""Editable table length, the report "Détails" link, and the Bibliothèque icons."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase
from django.urls import reverse

from powerbi_report import pagination
from powerbi_report.models import MetadataOption, ReportRef

User = get_user_model()


class PageSizeHelperTests(TestCase):
    def _request(self, query="", session=None):
        request = RequestFactory().get("/x/" + query)
        request.session = dict(session or {})
        return request

    def test_default_when_nothing_was_chosen(self):
        self.assertEqual(pagination.page_size(self._request()), 10)

    def test_a_valid_choice_is_used_and_remembered(self):
        request = self._request("?per_page=50")
        self.assertEqual(pagination.page_size(request), 50)
        self.assertEqual(request.session[pagination.SESSION_KEY], 50)
        self.assertEqual(pagination.page_size(self._request(session=request.session)), 50)

    def test_values_that_are_not_offered_are_ignored(self):
        for bad in ("7", "0", "-5", "abc", "", "100000", "10.5"):
            with self.subTest(bad):
                request = self._request(f"?per_page={bad}", session={pagination.SESSION_KEY: 25})
                self.assertEqual(pagination.page_size(request), 25)       # the earlier choice survives

    def test_a_corrupted_session_value_falls_back_to_the_default(self):
        self.assertEqual(pagination.page_size(self._request(session={pagination.SESSION_KEY: "lots"})), 10)


class TableLengthPagesTests(TestCase):
    """Reports and users tables: the "Lignes" picker changes how many rows are shown."""

    def setUp(self):
        self.admin = User.objects.create_superuser(username="boss", password="x", ad2000="boss1")
        self.client.force_login(self.admin)
        for i in range(27):
            ReportRef.objects.create(pbirs_id=f"r-{i:02d}", name=f"Rapport {i:02d}", path=f"/CBI/R{i:02d}")
        for i in range(14):
            User.objects.create_user(username=f"u{i:02d}", password="x", ad2000=f"u{i:02d}")

    def _rows(self, response, key="reports"):
        return len(response.context[key])

    def test_reports_list_default_then_chosen_length_is_kept(self):
        url = reverse("powerbi_report:report_list")
        self.assertEqual(self._rows(self.client.get(url)), 10)
        self.assertEqual(self._rows(self.client.get(url + "?per_page=25")), 25)
        self.assertEqual(self._rows(self.client.get(url)), 25)                    # remembered for the session
        self.assertEqual(self._rows(self.client.get(url + "?per_page=7")), 25)    # not an offered length

    def test_the_choice_is_shared_by_the_other_tables(self):
        self.client.get(reverse("powerbi_report:report_list") + "?per_page=25")
        flat = self.client.get(reverse("powerbi_report:report_list_flat"))
        self.assertEqual(self._rows(flat), 25)
        users = self.client.get(reverse("users_view") + "?per_page=10")
        self.assertEqual(self._rows(users, "users"), 10)
        self.assertEqual(self._rows(self.client.get(reverse("powerbi_report:report_list_flat"))), 10)

    def test_users_table_honours_the_length(self):
        response = self.client.get(reverse("users_view") + "?per_page=50")
        self.assertEqual(self._rows(response, "users"), response.context["users"].paginator.count)
        self.assertEqual(response.context["users"].paginator.num_pages, 1)

    def test_the_picker_is_rendered_with_the_current_length_selected(self):
        html = self.client.get(reverse("powerbi_report:report_list") + "?per_page=50").content.decode()
        self.assertIn("data-page-size", html)
        self.assertRegex(html, r'<option value="50" selected>50</option>')
        for size in (10, 25, 50, 100, 200):
            self.assertIn(f'<option value="{size}"', html)

    def test_the_picker_is_on_every_table(self):
        from users.utils import log_history
        for i in range(12):                                   # the history footer only shows with more than 10 rows
            log_history(self.admin, f"Rapport consulté {i}")
        for name in ("powerbi_report:report_list", "powerbi_report:report_list_flat", "users_view", "user_history"):
            with self.subTest(name):
                html = self.client.get(reverse(name) + "?per_page=10").content.decode()
                self.assertIn("data-page-size", html)

    def test_the_page_number_is_still_honoured(self):
        url = reverse("powerbi_report:report_list")
        response = self.client.get(url + "?per_page=10&page=3")
        self.assertEqual(response.context["reports"].number, 3)
        self.assertEqual(self._rows(response), 7)


class DetailsLinkTests(TestCase):
    def test_the_details_button_goes_to_the_report_page_instead_of_opening_the_modal(self):
        admin = User.objects.create_superuser(username="boss", password="x")
        self.client.force_login(admin)
        ReportRef.objects.create(pbirs_id="r-1", name="Ventes", path="/CBI/Ventes")
        html = self.client.get(reverse("powerbi_report:report_list")).content.decode()
        detail_url = reverse("powerbi_report:report_detail", args=["r-1"])
        self.assertRegex(html, rf'<a href="{detail_url}"[^>]*data-act="detail"')
        self.assertNotIn("data-open-report", html)


class BiblioBase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username="boss", password="x", ad2000="boss1")
        self.reader = User.objects.create_user(username="reader", password="x", ad2000="reader1", status="Active")
        self.finance = MetadataOption.objects.create(option_type="module", name="Finance")   # the library is grouped by module
        self.rh = MetadataOption.objects.create(option_type="module", name="Ressources humaines")
        self.reports = []
        for i, (name, direction) in enumerate([("Budget", self.finance), ("Marges", self.finance), ("Effectifs", self.rh)]):
            report = ReportRef.objects.create(pbirs_id=f"b{i}", name=name, path=f"/BI/{name}", report_type="bibliotheque")
            report.modules.set([direction])
            self.reports.append(report)
        patcher = patch("powerbi_report.views.get_visible_report_ids", return_value=["b0", "b1", "b2"])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.force_login(self.admin)

    def root(self):
        return self.client.get(reverse("powerbi_report:custom_biblio"))



class BiblioBrowsingTests(BiblioBase):
    """Same cards as the other sections: only the two icons differ."""

    def test_folders_and_reports_use_the_library_icons(self):
        html = self.root().content.decode()
        self.assertIn(">local_library<", html)
        self.assertNotIn(">folder<", html)
        folder = self.client.get(reverse("powerbi_report:custom_folder_detail", args=["biblio", self.finance.id]))
        html = folder.content.decode()
        self.assertIn(">menu_book<", html)
        self.assertNotIn(">bar_chart<", html)

    def test_other_sections_keep_their_icons(self):
        direction = MetadataOption.objects.create(option_type="direction", name="Direction générale")
        dashboard = ReportRef.objects.create(pbirs_id="d1", name="Tableau", path="/CBI/Tableau", report_type="dashboard")
        dashboard.directions.set([direction])
        with patch("powerbi_report.views.get_visible_report_ids", return_value=["d1"]):
            html = self.client.get(reverse("powerbi_report:custom_direction")).content.decode()
        self.assertIn(">folder<", html)
        self.assertNotIn(">local_library<", html)
