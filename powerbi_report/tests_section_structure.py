"""
The sections of the sidebar, as the product owner described them (all of it limited to what the user
may open in PBIRS, and all of it about dashboards except the library):

* Consolidé    : folders = directions, in them the reports ticked "consolidé"
* Pôle         : pôles -> only the sociétés attached to that pôle -> the directions of that société -> reports
* Direction    : folders = directions, in them every report of that direction
* Par Module   : folders = modules, in them the dashboards that have that module
* Bibliothèque : folders = modules, in them the library reports
"""
import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from powerbi_report.models import MetadataOption, ReportRef

User = get_user_model()


class SectionStructureTests(TestCase):
    def setUp(self):
        # A superuser sees every report the sections let through, so the real selection rules run.
        self.client.force_login(User.objects.create_superuser(username='boss', password='x', ad2000='boss1'))
        mk = lambda kind, name, **extra: MetadataOption.objects.create(option_type=kind, name=name, **extra)
        self.pole, self.other_pole = mk('pole', 'Production'), mk('pole', 'Services')
        self.mine = mk('societe', 'Alpha', parent=self.pole)
        self.theirs = mk('societe', 'Beta', parent=self.other_pole)
        self.unattached = mk('societe', 'Gamma')
        self.commerce, self.finance = mk('direction', 'Commerce'), mk('direction', 'Finance')
        self.achats, self.stock = mk('module', 'Achats'), mk('module', 'Entrepôt')

        def report(pbirs_id, name, rtype='dashboard', consolide=False, poles=(), societes=(), directions=(), modules=()):
            r = ReportRef.objects.create(pbirs_id=pbirs_id, name=name, path=f'/BI/{name}', report_type=rtype, is_consolide=consolide)
            r.poles.set(poles)
            r.societes.set(societes)
            r.directions.set(directions)
            r.modules.set(modules)
            return r

        self.report = report
        for target in ('requests.get', 'requests.post', 'requests.request'):
            patcher = patch(target, side_effect=AssertionError('PBIRS must not be contacted'))
            patcher.start()
            self.addCleanup(patcher.stop)

    def root(self, view):
        return self.client.get(reverse(f'powerbi_report:custom_{view}')).context

    def folder(self, view, option, query=''):
        return self.client.get(reverse('powerbi_report:custom_folder_detail', args=[view, option.id]) + query).context

    # ---- consolidé -------------------------------------------------------------------------
    def test_consolide_is_directions_holding_the_consolidated_reports_only(self):
        self.report('c1', 'Conso Commerce', consolide=True, directions=[self.commerce])
        self.report('c2', 'Simple Finance', directions=[self.finance])           # not consolidated
        self.assertEqual([f.name for f in self.root('consolide')['subfolders']], ['Commerce'])
        self.assertEqual([r['name'] for r in self.folder('consolide', self.commerce)['reports']], ['Conso Commerce'])

    def test_a_consolidated_report_without_a_direction_is_still_reachable(self):
        self.report('c3', 'Conso Groupe', consolide=True)
        loose = self.root('consolide')['loose_reports']
        self.assertEqual([(r['name'], r['label']) for r in loose], [('Conso Groupe', 'Sans direction')])

    def test_consolide_leaves_out_the_other_report_types(self):
        self.report('c4', 'Anomalie conso', rtype='anomalie', consolide=True, directions=[self.commerce])
        self.report('c5', 'Livre conso', rtype='bibliotheque', consolide=True)
        context = self.root('consolide')
        self.assertEqual(context['subfolders'], [])
        self.assertEqual(context['loose_reports'], [])

    # ---- pôle ------------------------------------------------------------------------------
    def test_a_pole_shows_only_the_societes_attached_to_it(self):
        self.report('p1', 'Chez Alpha', poles=[self.pole], societes=[self.mine])
        self.report('p2', 'Chez Beta', poles=[self.pole], societes=[self.theirs])      # Beta belongs to another pôle
        context = self.folder('pole', self.pole)
        self.assertEqual([f.name for f in context['subfolders']], ['Alpha'])
        self.assertEqual([r['name'] for r in context['reports']], ['Chez Beta'])        # listed here, never lost

    def test_a_societe_not_attached_to_any_pole_yet_still_appears_under_the_pole_of_its_reports(self):
        self.report('p3', 'Chez Gamma', poles=[self.pole], societes=[self.unattached])
        self.assertEqual([f.name for f in self.folder('pole', self.pole)['subfolders']], ['Gamma'])
        self.assertEqual(self.folder('pole', self.other_pole)['subfolders'], [])

    def test_pole_to_societe_to_direction_to_reports(self):
        self.report('p4', 'Ventes Alpha', poles=[self.pole], societes=[self.mine], directions=[self.commerce])
        self.report('p5', 'Marges Alpha', poles=[self.pole], societes=[self.mine], directions=[self.finance])
        self.assertEqual([f.name for f in self.root('pole')['subfolders']], ['Production'])
        societe = self.folder('pole', self.mine, f'?pole={self.pole.id}')
        self.assertEqual([f.name for f in societe['subfolders']], ['Commerce', 'Finance'])
        self.assertEqual(societe['reports'], [])
        direction = self.folder('pole', self.commerce, f'?pole={self.pole.id}&societe={self.mine.id}')
        self.assertEqual([r['name'] for r in direction['reports']], ['Ventes Alpha'])

    # ---- direction -------------------------------------------------------------------------
    def test_direction_is_directions_holding_every_report_of_that_direction(self):
        self.report('d1', 'Ventes', directions=[self.commerce])
        self.report('d2', 'Ventes conso', consolide=True, directions=[self.commerce])
        self.report('d3', 'Anomalie vente', rtype='anomalie', directions=[self.commerce])   # dashboards only
        self.assertEqual([f.name for f in self.root('direction')['subfolders']], ['Commerce'])
        self.assertEqual({r['name'] for r in self.folder('direction', self.commerce)['reports']}, {'Ventes', 'Ventes conso'})

    # ---- module and bibliothèque -----------------------------------------------------------
    def test_module_is_modules_holding_the_dashboards_that_have_that_module(self):
        self.report('m1', 'Suivi achats', modules=[self.achats])
        self.report('m2', 'Livre achats', rtype='bibliotheque', modules=[self.achats])
        self.report('m3', 'Dashboard sans module')
        self.assertEqual([f.name for f in self.root('module')['subfolders']], ['Achats'])
        self.assertEqual([r['name'] for r in self.folder('module', self.achats)['reports']], ['Suivi achats'])

    def test_bibliotheque_is_per_module_with_the_library_reports(self):
        self.report('b1', 'Livre achats', rtype='bibliotheque', modules=[self.achats])
        self.report('b2', 'Suivi stock', modules=[self.stock])                          # a dashboard: not in the library
        context = self.root('biblio')
        self.assertEqual([f.name for f in context['subfolders']], ['Achats'])
        self.assertEqual([r['name'] for r in self.folder('biblio', self.achats)['reports']], ['Livre achats'])

    def test_module_and_bibliotheque_folders_carry_a_small_distinguishing_tag(self):
        self.report('m4', 'Suivi achats', modules=[self.achats])
        self.report('b3', 'Livre achats', rtype='bibliotheque', modules=[self.achats])
        module_page = self.client.get(reverse('powerbi_report:custom_module')).content.decode()
        biblio_page = self.client.get(reverse('powerbi_report:custom_biblio')).content.decode()
        self.assertIn('data-folder-kind="module"', module_page)
        self.assertNotIn('data-folder-kind="biblio"', module_page)
        self.assertIn('data-folder-kind="biblio"', biblio_page)
        self.assertNotIn('data-folder-kind="module"', biblio_page)

    def test_the_direction_and_pole_sections_have_no_such_tag(self):
        self.report('d4', 'Ventes', poles=[self.pole], directions=[self.commerce])
        for view in ('direction', 'pole'):
            page = self.client.get(reverse(f'powerbi_report:custom_{view}')).content.decode()
            self.assertNotIn('data-folder-kind', page, view)


class FilterBarTests(TestCase):
    def test_the_report_list_filters_have_a_single_apply_button(self):
        self.client.force_login(User.objects.create_superuser(username='boss2', password='x', ad2000='boss2'))
        with patch('requests.get', side_effect=AssertionError('PBIRS must not be contacted')):
            html = self.client.get(reverse('powerbi_report:report_list')).content.decode()
        self.assertEqual(html.count('id="applyFiltersBtn"'), 1)
        self.assertEqual(len(re.findall(r'>\s*Appliquer[^<]*<', html)), 1)       # no second "Appliquer les filtres"
        self.assertNotIn('Appliquer les filtres', html)
        # it sits in the always-visible row, not inside the collapsible panel, and shows on phones too
        panel = html[html.index('id="advancedFiltersPanel"'):]
        self.assertNotIn('applyFiltersBtn', panel)
        self.assertNotIn('hidden sm:flex', html[html.index('id="applyFiltersBtn"') - 80:html.index('id="applyFiltersBtn"') + 400])
