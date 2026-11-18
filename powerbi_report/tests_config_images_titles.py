"""Config page (server dialogs), images on every metadata section, and the "Section | PBI" tab titles."""
import io
import re
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from powerbi_report.models import LOGO_OPTION_TYPES, MetadataOption, PBIRSServer, ReportRef

User = get_user_model()


def png(name="logo.png"):
    buffer = io.BytesIO()
    Image.new("RGBA", (40, 40), (10, 120, 200, 255)).save(buffer, format="PNG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


class ConfigPageTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username="boss", password="x", ad2000="boss1")
        self.client.force_login(self.admin)
        PBIRSServer.objects.create(name="PBIRS Production", base_url="https://bi.example.com/")

    def html(self):
        return self.client.get(reverse("powerbi_report:server_management_list")).content.decode()

    def test_every_element_the_edit_server_script_uses_exists(self):
        """The edit button did nothing because the script wrote to a field the dialog no longer had."""
        html = self.html()
        script = re.search(r"function editServer\(.*?\n    }\n", html, re.S).group(0)
        for element_id in re.findall(r"getElementById\('([^']+)'\)", script):
            self.assertIn(f'id="{element_id}"', html, element_id)

    def test_the_server_dialogs_are_complete(self):
        html = self.html()
        for modal in ("addServerModal", "editServerModal"):
            block = html[html.index(f'<div id="{modal}"'):]
            block = block[:block.index("<!-- ", 10)]
            self.assertIn('name="is_active"', block, modal)                # the "Serveur Actif" switch
            self.assertIn('type="submit"', block, modal)                   # and a way to save
            self.assertEqual(block.count("<div"), block.count("</div>"), modal)   # balanced markup
            self.assertEqual(block.count("<form"), block.count("</form>"), modal)

    def test_editing_a_server_keeps_it_active_when_the_switch_is_on(self):
        server = PBIRSServer.objects.get()
        self.client.post(reverse("powerbi_report:server_edit", args=[server.id]),
                         {"name": "PBIRS Prod", "base_url": "https://bi.example.com/", "is_active": "on"})
        server.refresh_from_db()
        self.assertEqual((server.name, server.is_active), ("PBIRS Prod", True))


class MetadataImagesTests(TestCase):
    def setUp(self):
        self.enterContext(override_settings(MEDIA_ROOT=tempfile.mkdtemp()))
        self.admin = User.objects.create_superuser(username="boss", password="x", ad2000="boss1")
        self.client.force_login(self.admin)
        self.options = {kind: MetadataOption.objects.create(option_type=kind, name=f"Mon {kind}")
                        for kind in ("pole", "direction", "societe", "module")}
        self.options["societe"].parent = self.options["pole"]
        self.options["societe"].save()
        report = ReportRef.objects.create(pbirs_id="r-1", name="Budget", path="/BI/Budget",
                                          report_type="dashboard", is_consolide=True)
        report.poles.set([self.options["pole"]])
        report.directions.set([self.options["direction"]])
        report.societes.set([self.options["societe"]])
        report.modules.set([self.options["module"]])
        patcher = patch("powerbi_report.views.get_visible_report_ids", return_value=["r-1"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def upload(self, kind):
        option = self.options[kind]
        data = {"name": option.name, "logo": png()}
        if kind == "societe":
            data["parent_id"] = self.options["pole"].id
        response = self.client.post(reverse("powerbi_report:metadata_option_edit", args=[option.id]), data)
        option.refresh_from_db()
        return response

    def test_all_four_sections_accept_an_image(self):
        self.assertEqual(set(LOGO_OPTION_TYPES), {"pole", "direction", "societe", "module"})
        for kind in LOGO_OPTION_TYPES:
            with self.subTest(kind):
                self.upload(kind)
                self.assertTrue(self.options[kind].logo, kind)

    def test_the_config_dialog_offers_the_image_field_for_every_section(self):
        html = self.client.get(reverse("powerbi_report:server_management_list")).content.decode()
        self.assertIn("container.classList.remove('hidden');   // every section can have an image", html)
        self.assertNotIn("type === 'pole' || type === 'societe'", html)
        for kind in ("direction", "module"):
            self.upload(kind)
        html = self.client.get(reverse("powerbi_report:server_management_list")).content.decode()
        for kind in ("direction", "module"):
            url = reverse("powerbi_report:metadata_option_logo", args=[self.options[kind].id])
            self.assertIn(f"editMetadataOption('{self.options[kind].id}', '{kind}'", html)
            self.assertGreaterEqual(html.count(url), 2)                  # thumbnail in the list + the edit button

    def test_the_image_is_shown_on_the_folder_cards_of_the_web_version(self):
        for kind in ("direction", "module", "pole"):
            self.upload(kind)
        for url_name, kind in (("custom_direction", "direction"), ("custom_module", "module"), ("custom_pole", "pole")):
            with self.subTest(kind):
                html = self.client.get(reverse(f"powerbi_report:{url_name}")).content.decode()
                logo_url = reverse("powerbi_report:metadata_option_logo", args=[self.options[kind].id])
                self.assertRegex(html, rf'<img src="{logo_url}\?v={kind}-[0-9a-f]+\.png"')

    def test_a_folder_without_image_keeps_its_icon(self):
        html = self.client.get(reverse("powerbi_report:custom_direction")).content.decode()
        self.assertIn(">folder<", html)
        self.assertNotIn("metadata_logos", html)

    def test_the_web_image_is_served_and_cacheable_for_signed_in_users_only(self):
        self.upload("direction")
        url = reverse("powerbi_report:metadata_option_logo", args=[self.options["direction"].id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("immutable", response["Cache-Control"])
        response.close()
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)          # to the login page


class SidebarTitleTests(TestCase):
    """The tab title of each sidebar section reads "Section | PBI"."""

    PAGES = [
        ("landing", "Accueil"),
        ("powerbi_report:dashboard", "Overview"),
        ("powerbi_report:custom_direction", "Direction"),
        ("powerbi_report:custom_pole", "Pôle"),
        ("powerbi_report:custom_consolide", "Consolidé"),
        ("powerbi_report:custom_biblio", "Bibliothèque"),
        ("powerbi_report:custom_module", "Module"),
        ("powerbi_report:custom_anomalie", "Anomalie"),
        ("powerbi_report:slider_list", "Slider"),
        ("users_view", "Utilisateurs"),
        ("powerbi_report:report_list", "Rapports"),
        ("powerbi_report:server_management_list", "Config"),
        ("user_history", "Historique"),
        ("tickets:ticket_list", "Tickets"),
    ]

    def setUp(self):
        self.admin = User.objects.create_superuser(username="boss", password="x", ad2000="boss1")
        self.client.force_login(self.admin)

    def title(self, url_name):
        response = self.client.get(reverse(url_name), follow=True)
        self.assertEqual(response.status_code, 200, url_name)
        return re.search(r"<title>(.*?)</title>", response.content.decode(), re.S).group(1).strip()

    def test_each_sidebar_section_is_titled_section_pipe_pbi(self):
        for url_name, label in self.PAGES:
            with self.subTest(url_name):
                self.assertEqual(self.title(url_name), f"{label} | PBI")

    def test_other_pages_keep_the_usual_suffix(self):
        self.assertTrue(self.title("notifications").endswith("| CBI"))
