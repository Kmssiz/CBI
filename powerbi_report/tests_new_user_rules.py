"""A new user is new in BOTH systems: recent in the directory (LDAP) AND recently hired in Odoo."""
from datetime import date, datetime, timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from powerbi_report.models import NewUserAlert, OdooProfile, OdooSettings
from powerbi_report.services.odoo_client import OdooEmployee
from powerbi_report.services.odoo_sync import sync_odoo_profiles
from users.directory_sync import sync_directory_users
from users.ldap_utils import parse_ad_time

User = get_user_model()


def employee(employee_id, name, hired):
    return OdooEmployee(employee_id=employee_id, full_name=name, post_title='Juriste', hire_date=hired, company='Giryad')


class NewInBothSystemsTests(TestCase):
    def setUp(self):
        OdooSettings.objects.update_or_create(pk=1, defaults={'new_users_since': timezone.now() - timedelta(days=400)})
        self.today = date.today()

    def person(self, ident, ad_created_days_ago=3):
        user = User.objects.create_user(username=ident, password='x', ad2000=ident)
        user.ldap_created_at = timezone.now() - timedelta(days=ad_created_days_ago)
        user.save(update_fields=['ldap_created_at'])
        return user

    def test_recent_in_the_directory_and_recently_hired_is_announced(self):
        user = self.person('N1')
        summary = sync_odoo_profiles([employee('N1', 'Nadia Un', self.today - timedelta(days=5))])
        self.assertEqual(summary.new_users, 1)
        self.assertTrue(NewUserAlert.objects.filter(user=user).exists())

    def test_hired_long_ago_is_not_new_even_if_cbi_only_just_saw_them(self):
        user = self.person('N2')          # account created yesterday here and in the directory...
        summary = sync_odoo_profiles([employee('N2', 'Omar Deux', self.today - timedelta(days=900))])   # ...but a veteran in Odoo
        self.assertEqual(summary.new_users, 0)
        self.assertFalse(NewUserAlert.objects.exists())
        self.assertEqual(OdooProfile.objects.get(user=user).hire_date, self.today - timedelta(days=900))   # their info is still gathered

    def test_an_old_directory_account_is_not_new_even_if_the_odoo_record_is(self):
        self.person('N3', ad_created_days_ago=700)
        self.assertEqual(sync_odoo_profiles([employee('N3', 'Sara Trois', self.today - timedelta(days=2))]).new_users, 0)

    def test_an_unknown_hire_date_is_never_guessed_as_new(self):
        self.person('N4')
        self.assertEqual(sync_odoo_profiles([employee('N4', 'Yanis Quatre', None)]).new_users, 0)

    def test_without_a_directory_date_the_cbi_account_date_is_used(self):
        user = User.objects.create_user(username='N5', password='x', ad2000='N5')      # ldap_created_at unknown, created now
        sync_odoo_profiles([employee('N5', 'Lina Cinq', self.today)])
        self.assertTrue(NewUserAlert.objects.filter(user=user).exists())

    def test_the_window_is_configurable(self):
        self.person('N6', ad_created_days_ago=10)
        with override_settings(NEW_USER_MAX_AGE_DAYS=5):
            self.assertEqual(sync_odoo_profiles([employee('N6', 'Karim Six', self.today - timedelta(days=10))]).new_users, 0)
        with override_settings(NEW_USER_MAX_AGE_DAYS=30):
            self.assertEqual(sync_odoo_profiles([employee('N6', 'Karim Six', self.today - timedelta(days=10))]).new_users, 1)

    def test_waiting_announcements_of_people_who_are_not_new_are_withdrawn_but_read_ones_stay(self):
        noisy = self.person('N7')
        read = self.person('N8')
        NewUserAlert.objects.create(user=noisy)
        NewUserAlert.objects.create(user=read, seen_at=timezone.now())
        summary = sync_odoo_profiles([employee('N7', 'Bruit Sept', self.today - timedelta(days=1200)),
                                      employee('N8', 'Lu Huit', self.today - timedelta(days=1200))])
        self.assertEqual(summary.retired, 1)
        self.assertFalse(NewUserAlert.objects.filter(user=noisy).exists())
        self.assertTrue(NewUserAlert.objects.filter(user=read).exists())

    def test_people_missing_from_this_odoo_run_keep_their_announcement(self):
        user = self.person('N9')
        NewUserAlert.objects.create(user=user)
        sync_odoo_profiles([])             # e.g. an empty answer: nothing may be withdrawn
        self.assertTrue(NewUserAlert.objects.filter(user=user).exists())


class DirectoryCreationDateTests(TestCase):
    def test_ad_times_are_read_in_both_forms_and_junk_is_ignored(self):
        expected = datetime(2026, 11, 5, 8, 30, 12, tzinfo=dt_timezone.utc)
        self.assertEqual(parse_ad_time('20261105083012.0Z'), expected)
        self.assertEqual(parse_ad_time(['20261105083012.0Z']), expected)
        self.assertEqual(parse_ad_time(expected), expected)
        self.assertEqual(parse_ad_time(datetime(2026, 11, 5, 8, 30)).tzinfo, dt_timezone.utc)
        for junk in (None, '', 'n/a', 123, '2026-11-05'):
            self.assertIsNone(parse_ad_time(junk))

    def test_the_directory_sync_stores_the_creation_date_for_new_and_existing_accounts(self):
        created = datetime(2026, 11, 1, tzinfo=dt_timezone.utc)
        row = {'sAMAccountName': 'ldap1', 'ad2000': 'LD1', 'name': 'Lea Test', 'created': created}
        sync_directory_users([row])
        self.assertEqual(User.objects.get(ad2000='LD1').ldap_created_at, created)
        User.objects.filter(ad2000='LD1').update(ldap_created_at=None)
        sync_directory_users([row])
        self.assertEqual(User.objects.get(ad2000='LD1').ldap_created_at, created)
