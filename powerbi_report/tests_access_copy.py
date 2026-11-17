"""Copying one user's access (reports, RLS, view rights) to another. PBIRS is replaced by an in-memory fake."""
import copy
from unittest.mock import patch

from django.contrib import messages as django_messages
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from notifications.models import Notification
from powerbi_report.models import ReportRef, UserReportPermission
from powerbi_report.services import access_copy
from powerbi_report.services.pbirs_client import PBIRSClientError
from users.models import Role

User = get_user_model()
DOMAIN = "GSH"


class FakePBIRS:
    """Policies and RLS of some reports, kept in memory, with the failure modes of the real client."""

    def __init__(self):
        self.policies, self.roles, self.assignments = {}, {}, {}
        self.unreadable, self.write_fails, self.rls_unreadable = set(), set(), set()
        self.policy_writes, self.rls_writes = [], []

    # policies
    def get_report_policies_strict(self, report_id, base_url=None):
        if report_id in self.unreadable:
            raise PBIRSClientError("cannot read")
        return copy.deepcopy(self.policies.get(report_id, []))

    def set_report_policies_strict(self, report_id, policies, base_url=None):
        if report_id in self.write_fails:
            raise PBIRSClientError("refused")
        self.policy_writes.append(report_id)
        self.policies[report_id] = copy.deepcopy(policies)

    # RLS
    def get_data_model_roles_strict(self, report_id, base_url=None):
        if report_id in self.rls_unreadable:
            raise PBIRSClientError("rls unreadable")
        return copy.deepcopy(self.roles.get(report_id, []))

    def get_role_assignments_strict(self, report_id, base_url=None):
        return copy.deepcopy(self.assignments.get(report_id, []))

    def set_role_assignments_strict(self, report_id, assignments, base_url=None):
        self.rls_writes.append(report_id)
        self.assignments[report_id] = copy.deepcopy(assignments)


def role(rid, name):
    return {"ModelRoleId": rid, "ModelRoleName": name}


class AccessCopyBase(TestCase):
    def setUp(self):
        self.source = User.objects.create_user(username="alice", password="x", first_name="Alice", last_name="A", ad2000="alice1")
        self.target = User.objects.create_user(username="bob", password="x", first_name="Bob", last_name="B", ad2000="bob1")
        self.pbirs = FakePBIRS()
        self.reports = {n: ReportRef.objects.create(pbirs_id=f"id-{n}", name=f"Rapport {n}", path=f"/CBI/{n}") for n in "ABCD"}

    def give(self, user, name, direct=True):
        UserReportPermission.objects.create(user=user, report=self.reports[name], is_direct=direct)

    def plan(self, **kw):
        return access_copy.build_plan(lambda: self.pbirs, self.source, self.target, **kw)

    def apply(self, plan, **kw):
        return access_copy.apply_plan(lambda: self.pbirs, plan, **kw)


class PlanTests(AccessCopyBase):
    def test_only_direct_access_the_target_lacks_is_to_grant(self):
        for n in "ABC":
            self.give(self.source, n)
        self.give(self.source, "D", direct=False)         # through a group
        self.give(self.target, "B")                       # already there
        plan = self.plan(do_rls=False)
        self.assertEqual([r.pbirs_id for r in plan.to_grant], ["id-A", "id-C"])
        self.assertEqual((plan.already_have, plan.group_only), (1, 1))
        self.assertFalse(plan.is_empty)

    def test_nothing_is_written_while_planning(self):
        self.give(self.source, "A")
        self.plan()
        self.assertEqual(self.pbirs.policy_writes, [])
        self.assertEqual(self.pbirs.rls_writes, [])
        self.assertFalse(UserReportPermission.objects.filter(user=self.target).exists())

    def test_a_source_with_nothing_gives_an_empty_plan(self):
        self.assertTrue(self.plan().is_empty)

    def test_roles_follow_the_target_like_a_normal_grant(self):
        self.assertEqual(self.plan().roles, ["Explorateur"])
        admin_role = Role.objects.create(name="admin")
        self.target.role = admin_role
        self.target.save()
        plan = self.plan()
        self.assertEqual(plan.roles[0], "Explorateur")
        self.assertIn("Gestionnaire de contenu", plan.roles)
        self.assertTrue(plan.elevated)

    def test_the_options_limit_what_is_planned(self):
        self.give(self.source, "A")
        self.source.can_view_pole = True
        self.source.save()
        self.assertEqual(self.plan(do_reports=False, do_rls=False).to_grant, [])
        self.assertEqual(self.plan(do_reports=False, do_rls=False, do_sections=True).sections, [("can_view_pole", "Pôle")])
        self.assertEqual(self.plan(do_reports=False, do_rls=False, do_sections=False).sections, [])


class ApplyReportsTests(AccessCopyBase):
    def setUp(self):
        super().setUp()
        for n in "ABC":
            self.give(self.source, n)
            self.pbirs.policies[f"id-{n}"] = [{"GroupUserName": "someoneelse", "Roles": [{"Name": "Explorateur"}]}]

    def test_the_target_is_added_with_the_explorer_role_and_nobody_else_is_touched(self):
        result = self.apply(self.plan(do_rls=False))
        self.assertEqual(len(result.granted), 3)
        for n in "ABC":
            policies = self.pbirs.policies[f"id-{n}"]
            self.assertEqual(policies[0], {"GroupUserName": "someoneelse", "Roles": [{"Name": "Explorateur"}]})
            self.assertEqual(policies[1], {"GroupUserName": "bob1", "Roles": [{"Name": "Explorateur"}]})
        self.assertEqual(
            set(UserReportPermission.objects.filter(user=self.target, is_direct=True).values_list("report__pbirs_id", flat=True)),
            {"id-A", "id-B", "id-C"},
        )

    def test_an_admin_target_also_gets_the_admin_roles(self):
        self.target.role = Role.objects.create(name="admin")
        self.target.save()
        self.apply(self.plan(do_rls=False))
        names = [r["Name"] for r in self.pbirs.policies["id-A"][1]["Roles"]]
        self.assertEqual(names, ["Explorateur", "Gestionnaire de contenu", "Mes rapports", "Report Builder", "Serveur de publication"])

    def test_running_it_twice_changes_nothing_the_second_time(self):
        self.apply(self.plan(do_rls=False))
        writes = len(self.pbirs.policy_writes)
        second = self.plan(do_rls=False)
        self.assertEqual((second.to_grant, second.already_have), ([], 3))
        self.apply(second)
        self.assertEqual(len(self.pbirs.policy_writes), writes)

    def test_a_policy_pbirs_already_has_is_not_rewritten_nor_counted_as_granted(self):
        self.pbirs.policies["id-B"].append({"GroupUserName": "GSH\\Bob1", "Roles": [{"Name": "Explorateur"}]})
        result = self.apply(self.plan(do_rls=False))       # the local cache did not know about it
        self.assertEqual([r.pbirs_id for r in result.granted], ["id-A", "id-C"])
        self.assertEqual([r.pbirs_id for r in result.already_present], ["id-B"])
        self.assertNotIn("id-B", self.pbirs.policy_writes)
        self.assertTrue(UserReportPermission.objects.filter(user=self.target, report=self.reports["B"]).exists())

    def test_an_unreadable_report_is_reported_and_never_written(self):
        self.pbirs.unreadable.add("id-B")
        result = self.apply(self.plan(do_rls=False))
        self.assertEqual([r.pbirs_id for r in result.granted], ["id-A", "id-C"])
        self.assertEqual([r.pbirs_id for r, _ in result.grant_failed], ["id-B"])
        self.assertNotIn("id-B", self.pbirs.policy_writes)
        self.assertEqual(self.pbirs.policies["id-B"], [{"GroupUserName": "someoneelse", "Roles": [{"Name": "Explorateur"}]}])
        self.assertFalse(UserReportPermission.objects.filter(user=self.target, report=self.reports["B"]).exists())

    def test_a_refused_write_is_reported_and_the_others_go_on(self):
        self.pbirs.write_fails.add("id-A")
        result = self.apply(self.plan(do_rls=False))
        self.assertEqual([r.pbirs_id for r, _ in result.grant_failed], ["id-A"])
        self.assertEqual(len(result.granted), 2)

    def test_when_the_time_budget_is_spent_the_rest_is_left_for_a_second_run(self):
        result = self.apply(self.plan(do_rls=False), time_budget=-1)
        self.assertEqual((len(result.granted), result.not_processed), (0, 3))
        self.assertEqual(self.pbirs.policy_writes, [])
        again = self.plan(do_rls=False)
        self.assertEqual(len(again.to_grant), 3)           # nothing was lost: a re-run finishes the job


class RlsTests(AccessCopyBase):
    def setUp(self):
        super().setUp()
        for n in "AC":
            self.give(self.source, n)
        self.pbirs.roles = {"id-A": [role("r1", "Ventes"), role("r2", "Achats")], "id-C": [role("r1", "Nord")]}
        self.pbirs.assignments = {
            "id-A": [{"GroupUserName": "GSH\\alice1", "DataModelRoles": ["r1", "r2"]},
                     {"GroupUserName": "GSH\\carol1", "DataModelRoles": ["r1"]}],
            "id-C": [{"GroupUserName": "GSH\\alice1", "DataModelRoles": ["r1"]}],
        }
        for n in "AC":
            self.pbirs.policies[f"id-{n}"] = []

    def test_the_plan_lists_the_roles_to_add_per_report(self):
        plan = self.plan(do_reports=True, do_rls=True)
        self.assertEqual({i.report.pbirs_id: i.role_names for i in plan.rls}, {"id-A": ["Ventes", "Achats"], "id-C": ["Nord"]})

    def test_rls_is_applied_next_to_the_access_and_other_peoples_roles_are_kept(self):
        result = self.apply(self.plan())
        self.assertEqual(sorted(r.pbirs_id for r, _ in result.rls_done), ["id-A", "id-C"])
        from powerbi_report.services.rls import short_name
        by_user = {short_name(a["GroupUserName"]): a["DataModelRoles"] for a in self.pbirs.assignments["id-A"]}
        self.assertEqual(by_user["carol1"], ["r1"])
        self.assertEqual(sorted(by_user["bob1"]), ["r1", "r2"])
        self.assertEqual(by_user["alice1"], ["r1", "r2"])               # the source keeps hers

    def test_roles_the_target_already_has_are_not_planned_again(self):
        self.pbirs.assignments["id-A"].append({"GroupUserName": "GSH\\bob1", "DataModelRoles": ["r1"]})
        plan = self.plan()
        item = next(i for i in plan.rls if i.report.pbirs_id == "id-A")
        self.assertEqual(item.role_names, ["Achats"])                  # only the missing one
        self.apply(plan)
        mine = next(a for a in self.pbirs.assignments["id-A"] if a["GroupUserName"] == "GSH\\bob1")
        self.assertEqual(sorted(mine["DataModelRoles"]), ["r1", "r2"])

    def test_nothing_is_planned_when_the_target_already_has_every_role(self):
        for rid, roles in (("id-A", ["r1", "r2"]), ("id-C", ["r1"])):
            self.pbirs.assignments[rid].append({"GroupUserName": "GSH\\bob1", "DataModelRoles": roles})
        plan = self.plan()
        self.assertEqual(plan.rls, [])
        self.assertEqual(plan.rls_already, 2)

    def test_rls_is_not_copied_on_a_report_the_target_cannot_open(self):
        self.give(self.source, "D", direct=False)                      # group access: not copied, so no access for the target
        self.pbirs.roles["id-D"] = [role("r1", "Sud")]
        self.pbirs.assignments["id-D"] = [{"GroupUserName": "GSH\\alice1", "DataModelRoles": ["r1"]}]
        self.assertNotIn("id-D", [i.report.pbirs_id for i in self.plan().rls])
        self.give(self.target, "D", direct=False)                      # once the target has access through a group, it is
        self.assertIn("id-D", [i.report.pbirs_id for i in self.plan().rls])

    def test_a_report_whose_rls_cannot_be_read_is_listed_and_skipped(self):
        self.pbirs.rls_unreadable.add("id-C")
        plan = self.plan()
        self.assertEqual([r.pbirs_id for r in plan.rls_unreadable], ["id-C"])
        self.assertEqual([i.report.pbirs_id for i in plan.rls], ["id-A"])

    def test_reports_without_rls_roles_cost_nothing(self):
        self.pbirs.roles.pop("id-C")
        self.assertEqual([i.report.pbirs_id for i in self.plan().rls], ["id-A"])

    def test_the_rls_of_a_report_that_could_not_be_granted_is_not_applied(self):
        self.pbirs.write_fails.add("id-A")
        result = self.apply(self.plan())
        self.assertEqual([r.pbirs_id for r, _ in result.rls_done], ["id-C"])
        self.assertNotIn("id-A", self.pbirs.rls_writes)


class SectionTests(AccessCopyBase):
    def test_sections_are_only_switched_on_and_the_role_is_never_copied(self):
        self.source.can_view_pole = self.source.can_use_slider = True
        self.source.role = Role.objects.create(name="admin")
        self.source.save()
        self.target.can_view_direction = True
        self.target.save()
        result = self.apply(self.plan(do_reports=False, do_rls=False, do_sections=True))
        self.target.refresh_from_db()
        self.assertTrue(self.target.can_view_pole and self.target.can_use_slider)
        self.assertTrue(self.target.can_view_direction)                # his own right is kept
        self.assertIsNone(self.target.role)                            # an admin source does not make an admin
        self.assertEqual(sorted(result.sections_set), ["Pôle", "Slider"])


class ViewTests(AccessCopyBase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser(username="boss", password="x", ad2000="boss1")
        self.client.force_login(self.admin)
        for n in "AB":
            self.give(self.source, n)
            self.pbirs.policies[f"id-{n}"] = []
        patcher = patch("powerbi_report.views_modules.access_copy.PBIRSClient", lambda request: self.pbirs)
        patcher.start()
        self.addCleanup(patcher.stop)
        pw = patch("powerbi_report.views_modules.access_copy.get_ldap_password", lambda request: "secret")
        pw.start()
        self.addCleanup(pw.stop)

    def _preview(self, **extra):
        data = {"direction": "from_me", "other": "Bob B (bob1)", "do_reports": "on", "do_rls": "on"}
        data.update(extra)
        data = {k: v for k, v in data.items() if v is not None}
        return self.client.post(reverse("powerbi_report:copy_access_preview", args=["alice1"]), data)

    def _apply(self, **extra):
        data = {"source_user": "alice1", "target_user": "bob1", "do_reports": "on", "do_rls": "on"}
        data.update(extra)
        return self.client.post(reverse("powerbi_report:copy_access_apply", args=["alice1"]), data)

    def test_only_admins_can_use_it(self):
        self.client.force_login(self.target)
        self.assertEqual(self._preview().status_code, 403)
        self.assertEqual(self._apply().status_code, 403)
        self.assertEqual(self.pbirs.policy_writes, [])

    def test_the_preview_shows_what_will_happen_and_writes_nothing(self):
        response = self._preview()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Rapport A")
        self.assertContains(response, "Rapport B")
        self.assertContains(response, "Confirmer la copie")
        self.assertEqual(len(response.context["plan"].to_grant), 2)
        self.assertEqual(self.pbirs.policy_writes, [])
        self.assertEqual(self.pbirs.rls_writes, [])
        self.assertFalse(UserReportPermission.objects.filter(user=self.target).exists())
        self.assertEqual(Notification.objects.filter(user=self.target).count(), 0)

    def test_the_direction_can_be_reversed(self):
        self.give(self.target, "C")
        response = self.client.post(reverse("powerbi_report:copy_access_preview", args=["alice1"]), {
            "direction": "to_me", "other": "bob1", "do_reports": "on"})
        self.assertEqual(response.context["source"], self.target)       # Bob's access goes to Alice
        self.assertEqual(response.context["target"], self.source)
        self.assertEqual([r.pbirs_id for r in response.context["plan"].to_grant], ["id-C"])

    def test_applying_copies_notifies_and_returns_to_the_target_page(self):
        response = self._apply()
        self.assertRedirects(response, reverse("powerbi_report:user_permission", args=["bob1"]), fetch_redirect_response=False)
        self.assertEqual(sorted(self.pbirs.policy_writes), ["id-A", "id-B"])
        note = Notification.objects.get(user=self.target)
        self.assertIn("2 rapport(s)", note.message)
        self.assertIn("Rapport A", note.message)
        self.assertTrue(Notification.objects.filter(user=self.admin, message__contains="Alice A").exists())
        texts = [str(m) for m in django_messages.get_messages(response.wsgi_request)]
        self.assertTrue(any("Copie terminée" in t and "2 rapport(s) ajouté(s)" in t for t in texts), texts)

    def test_applying_twice_adds_nothing_the_second_time(self):
        self._apply()
        writes = len(self.pbirs.policy_writes)
        response = self._apply()
        self.assertEqual(len(self.pbirs.policy_writes), writes)
        texts = [str(m) for m in django_messages.get_messages(response.wsgi_request)]
        self.assertTrue(any("Rien à copier" in t for t in texts), texts)

    def test_a_failure_is_shown_as_an_error_and_the_rest_still_goes_through(self):
        self.pbirs.unreadable.add("id-A")
        response = self._apply()
        texts = [str(m) for m in django_messages.get_messages(response.wsgi_request)]
        self.assertTrue(any("n'ont pas pu être ajoutés" in t and "Rapport A" in t for t in texts), texts)
        self.assertEqual(self.pbirs.policy_writes, ["id-B"])

    def test_bad_requests_are_refused_without_touching_pbirs(self):
        cases = {
            "the same person": dict(other="Alice A (alice1)"),
            "an unknown person": dict(other="Nobody (zzz)"),
            "nothing to copy": dict(do_reports=None, do_rls=None),
        }
        for label, extra in cases.items():
            with self.subTest(label):
                response = self._preview(**extra)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(self.pbirs.policy_writes, [])
        User.objects.create_user(username="noad", password="x")                 # no AD identifier
        self.assertEqual(self._preview(other="noad").status_code, 302)

    def test_without_a_pbirs_session_nothing_happens(self):
        with patch("powerbi_report.views_modules.access_copy.get_ldap_password", lambda request: None):
            response = self._apply()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.pbirs.policy_writes, [])

    def test_preview_and_apply_only_accept_post(self):
        self.assertEqual(self.client.get(reverse("powerbi_report:copy_access_preview", args=["alice1"])).status_code, 405)
        self.assertEqual(self.client.get(reverse("powerbi_report:copy_access_apply", args=["alice1"])).status_code, 405)

    def test_the_picker_accepts_a_label_an_identifier_or_a_username(self):
        from powerbi_report.views_modules.access_copy import find_user
        self.assertEqual(find_user("Bob B (bob1)"), self.target)
        self.assertEqual(find_user("BOB1"), self.target)
        self.assertEqual(find_user("bob"), self.target)
        self.assertIsNone(find_user("nobody"))
        self.assertIsNone(find_user(""))
        self.target.is_active = False
        self.target.save()
        self.assertIsNone(find_user("bob1"))                            # nobody inactive

    def test_the_access_page_offers_the_copy_dialog(self):
        with patch("powerbi_report.views.get_powerbi_reports", return_value=[]):
            response = self.client.get(reverse("powerbi_report:user_permission", args=["alice1"]))
        html = response.content.decode()
        self.assertIn('id="copyAccessButton"', html)
        self.assertIn('id="copyAccessModal"', html)
        self.assertNotIn('<option value="Bob B (bob1)">', html)          # nobody is embedded: thousands of users
        self.assertIn(reverse("powerbi_report:copy_user_search"), html)  # the dialog searches as you type


class PeopleSearchTests(AccessCopyBase):
    """The dialog finds people as you type; the page never embeds the whole directory."""

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser(username="boss", password="x", ad2000="boss1")
        self.client.force_login(self.admin)
        User.objects.create_user(username="bdurand", password="x", first_name="Bob", last_name="Durand", ad2000="bd77")
        User.objects.create_user(username="gone", password="x", first_name="Bob", last_name="Parti", ad2000="gone1", is_active=False)
        User.objects.create_user(username="noad", password="x", first_name="Bob", last_name="SansAD")

    def search(self, q, **extra):
        response = self.client.get(reverse("powerbi_report:copy_user_search"), {"q": q, **extra})
        return response.json()["results"]

    def test_it_matches_names_usernames_and_identifiers_and_every_word(self):
        self.assertEqual(sorted(self.search("bob")), ["Bob B (bob1)", "Bob Durand (bd77)"])
        self.assertEqual(self.search("bob dur"), ["Bob Durand (bd77)"])       # every word must match
        self.assertEqual(self.search("bd77"), ["Bob Durand (bd77)"])
        self.assertEqual(self.search("BDURAND"), ["Bob Durand (bd77)"])

    def test_inactive_people_and_people_without_an_identifier_are_never_offered(self):
        names = " ".join(self.search("bob"))
        self.assertNotIn("Parti", names)
        self.assertNotIn("SansAD", names)

    def test_the_person_whose_page_it_is_is_left_out(self):
        self.assertEqual(self.search("bob", exclude="bob1"), ["Bob Durand (bd77)"])

    def test_it_waits_for_two_characters_and_caps_the_answer(self):
        self.assertEqual(self.search("b"), [])
        self.assertEqual(self.search(""), [])
        for i in range(30):
            User.objects.create_user(username=f"many{i}", password="x", first_name="Many", ad2000=f"many{i}")
        self.assertEqual(len(self.search("many")), 15)

    def test_only_admins_may_search_and_only_with_get(self):
        self.assertEqual(self.client.post(reverse("powerbi_report:copy_user_search"), {"q": "bob"}).status_code, 405)
        self.client.force_login(self.target)
        self.assertEqual(self.client.get(reverse("powerbi_report:copy_user_search"), {"q": "bob"}).status_code, 403)

    def test_directory_conflict_records_and_line_breaks_are_not_offered(self):
        User.objects.create_user(username="cnf1", password="x", first_name="Zed\nCNF:9928", last_name="Z", ad2000="$DUPLICATE-6065")
        User.objects.create_user(username="cnf2", password="x", first_name="Zed", last_name="Dup", ad2000="x-duplicate-9")
        User.objects.create_user(username="zed", password="x", first_name="Zed", last_name="Vrai", ad2000="zed1")
        self.assertEqual(self.search("zed"), ["Zed Vrai (zed1)"])

    def test_labels_are_always_on_one_line(self):
        User.objects.create_user(username="odd", password="x", first_name="Odd\nName", last_name="  Spaced ", ad2000="odd1")
        self.assertEqual(self.search("odd"), ["Odd Name Spaced (odd1)"])


class ConfirmFormTests(AccessCopyBase):
    def test_the_confirm_form_has_no_field_that_hides_a_form_property(self):
        """An input named 'target' hides form.target in the browser and used to switch the button spinner off."""
        admin = User.objects.create_superuser(username="boss", password="x", ad2000="boss1")
        self.client.force_login(admin)
        with patch("powerbi_report.views_modules.access_copy.PBIRSClient", lambda request: FakePBIRS()), \
             patch("powerbi_report.views_modules.access_copy.get_ldap_password", lambda request: "x"):
            html = self.client.post(reverse("powerbi_report:copy_access_preview", args=["alice1"]),
                                    {"direction": "from_me", "other": "bob1", "do_reports": "on"}).content.decode()
        for clobbering in ("target", "action", "method", "submit", "name", "id"):
            self.assertNotIn(f'name="{clobbering}"', html)
        self.assertIn('name="source_user"', html)
        self.assertIn('name="target_user"', html)


class SectionPreviewTests(AccessCopyBase):
    """The sections a person sees are copied on request, and the preview says which ones change."""

    def setUp(self):
        super().setUp()
        for field in ("can_view_direction", "can_view_pole", "can_view_consolide"):
            setattr(self.source, field, True)
        self.source.save()
        self.target.can_view_direction = True               # already sees Direction
        self.target.can_view_anomalie = True                # sees something the source does not: kept
        self.target.save()
        self.admin = User.objects.create_superuser(username="boss", password="x", ad2000="boss1")
        self.client.force_login(self.admin)
        for patcher in (patch("powerbi_report.views_modules.access_copy.PBIRSClient", lambda request: FakePBIRS()),
                        patch("powerbi_report.views_modules.access_copy.get_ldap_password", lambda request: "x")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_plan_separates_what_changes_from_what_is_already_visible(self):
        plan = self.plan(do_reports=False, do_rls=False, do_sections=True)
        self.assertEqual(sorted(label for _, label in plan.sections), ["Consolidé", "Pôle"])
        self.assertEqual(plan.sections_already, ["Direction", "Module"])      # Module is on for everybody by default
        self.assertFalse(plan.is_empty)

    def test_the_preview_lists_both_groups(self):
        response = self.client.post(reverse("powerbi_report:copy_access_preview", args=["alice1"]), {
            "direction": "from_me", "other": "bob1", "do_sections": "on"})
        html = response.content.decode()
        self.assertIn("Sections visibles dans le menu", html)
        self.assertIn("Seront activées", html)
        self.assertIn("Déjà visibles", html)
        self.assertIn('name="do_sections"', html)           # carried to the confirm step

    def test_confirming_switches_the_missing_sections_on_and_keeps_the_rest(self):
        self.client.post(reverse("powerbi_report:copy_access_apply", args=["alice1"]), {
            "source_user": "alice1", "target_user": "bob1", "do_sections": "on"})
        self.target.refresh_from_db()
        self.assertTrue(self.target.can_view_pole and self.target.can_view_consolide and self.target.can_view_direction)
        self.assertTrue(self.target.can_view_anomalie)       # his own section is not taken away
        self.assertIsNone(self.target.role)

    def test_the_dialog_offers_the_sections_and_ticks_them_by_default(self):
        with patch("powerbi_report.views.get_powerbi_reports", return_value=[]):
            html = self.client.get(reverse("powerbi_report:user_permission", args=["alice1"])).content.decode()
        self.assertRegex(html, r'<input type="checkbox" name="do_sections" checked>\s*Sections visibles')


class RestrictPlanTests(TestCase):
    """Unticking on the preview: a report and its RLS go together."""

    def _plan(self):
        from types import SimpleNamespace as NS
        a, b = NS(pk=1, name="A"), NS(pk=2, name="B")
        return access_copy.CopyPlan(
            source=None, target=None, to_grant=[a, b],
            rls=[access_copy.RlsItem(report=a, role_ids=["r"], role_names=["R"]),
                 access_copy.RlsItem(report=b, role_ids=["r"], role_names=["R"])],
        )

    def test_unticked_report_takes_its_rls_with_it(self):
        plan = access_copy.restrict_plan(self._plan(), skip_grant=["1"])
        self.assertEqual([r.pk for r in plan.to_grant], [2])
        self.assertEqual([i.report.pk for i in plan.rls], [2])

    def test_unticked_rls_alone_keeps_the_report(self):
        plan = access_copy.restrict_plan(self._plan(), skip_rls=["2"])
        self.assertEqual([r.pk for r in plan.to_grant], [1, 2])
        self.assertEqual([i.report.pk for i in plan.rls], [1])

    def test_nothing_unticked_changes_nothing(self):
        plan = access_copy.restrict_plan(self._plan())
        self.assertEqual(len(plan.to_grant), 2)
        self.assertEqual(len(plan.rls), 2)


class RolePickTests(TestCase):
    """The RLS dropdown on the preview: the admin picks the groups a line gives."""

    def _plan(self):
        from types import SimpleNamespace as NS
        a = NS(pk=1, name="A")
        available = [("r1", "Direction"), ("r2", "Pôle"), ("r3", "Société")]
        return access_copy.CopyPlan(
            source=None, target=None, to_grant=[a],
            rls=[access_copy.RlsItem(report=a, role_ids=["r1"], role_names=["Direction"], available=available)],
        )

    def test_picked_roles_replace_the_source_roles(self):
        plan = access_copy.restrict_plan(self._plan(), role_picks={1: ["r2", "r3"]})
        self.assertEqual(plan.rls[0].role_ids, ["r2", "r3"])
        self.assertEqual(plan.rls[0].role_names, ["Pôle", "Société"])

    def test_roles_the_report_does_not_define_are_ignored(self):
        plan = access_copy.restrict_plan(self._plan(), role_picks={1: ["hacked", "r3"]})
        self.assertEqual(plan.rls[0].role_ids, ["r3"])

    def test_a_line_left_without_role_is_dropped(self):
        plan = access_copy.restrict_plan(self._plan(), role_picks={1: []})
        self.assertEqual(plan.rls, [])
