from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.messages import get_messages
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from ldap3.core.exceptions import LDAPSocketOpenError

from users.ldap_utils import get_ad_users
from users.models import CustomUser, Role


class UserManagementAndSyncTests(TestCase):
    def setUp(self) -> None:
        self.admin_role = Role.objects.create(name=settings.ADMIN_ROLE_NAME)
        self.user_role = Role.objects.create(name=settings.USER_ROLE_NAME)

        self.admin_user = CustomUser.objects.create_user(
            username="admin_user",
            password="password123",
            role=self.admin_role,
            status="Active",
        )
        self.regular_user = CustomUser.objects.create_user(
            username="regular_user",
            password="password123",
            role=self.user_role,
            ad2000="REG001",
            status="Active",
        )

    def test_users_view_fetches_users_for_admin(self) -> None:
        self.client.login(username="admin_user", password="password123")

        response = self.client.get(reverse("users_view"))

        self.assertEqual(response.status_code, 200)
        users_page = response.context["users"]
        fetched_usernames = {user.username for user in users_page.object_list}
        self.assertIn("admin_user", fetched_usernames)
        self.assertIn("regular_user", fetched_usernames)

    @override_settings(LDAP_SERVICE_USERNAME="svc_user", LDAP_SERVICE_PASSWORD="svc_password")
    @patch("users.views.get_ad_users", return_value=[])
    def test_sync_users_shows_error_when_ldap_fetch_returns_no_users(self, mock_get_ad_users) -> None:
        self.client.login(username="admin_user", password="password123")

        response = self.client.get(reverse("sync_users"), follow=True)

        self.assertEqual(response.status_code, 200)
        mock_get_ad_users.assert_called_once_with("svc_user", "svc_password")

        flash_messages = [message.message for message in get_messages(response.wsgi_request)]
        self.assertIn(
            "Échec de la récupération des utilisateurs LDAP ou aucun utilisateur trouvé.",
            flash_messages,
        )

    @override_settings(LDAP_SERVICE_USERNAME="svc_user", LDAP_SERVICE_PASSWORD="svc_password")
    @patch("users.views.get_ad_users")
    def test_sync_users_creates_user_when_ldap_data_is_valid(self, mock_get_ad_users) -> None:
        mock_get_ad_users.return_value = [
            {
                "sAMAccountName": "newldapuser",
                "ad2000": "",
                "company": "CBI",
                "name": "New Ldap",
                "mail": "newldap@company.com",
                "ad_groups": ["Finance", "BI"],
            }
        ]
        self.client.login(username="admin_user", password="password123")

        response = self.client.get(reverse("sync_users"), follow=True)

        self.assertEqual(response.status_code, 200)
        created = CustomUser.objects.get(username="newldapuser")
        self.assertEqual(created.ad2000, "newldapuser")
        self.assertEqual(created.societe, "CBI")
        self.assertEqual(created.email, "newldap@company.com")
        self.assertEqual(created.first_name, "New")
        self.assertEqual(created.last_name, "Ldap")
        self.assertEqual(created.status, "Not Active")
        self.assertEqual(created.role.name, settings.USER_ROLE_NAME)
        self.assertEqual(created.ad_groups, ["Finance", "BI"])

        flash_messages = [message.message for message in get_messages(response.wsgi_request)]
        self.assertTrue(
            any("Synchronisation des utilisateurs terminée. 1 nouveaux utilisateurs ajoutés." in msg for msg in flash_messages)
        )

    def test_sync_users_non_admin_redirects_without_crashing(self) -> None:
        user_without_role = CustomUser.objects.create_user(
            username="no_role_user",
            password="password123",
        )
        self.client.force_login(user_without_role)

        response = self.client.get(reverse("sync_users"))

        self.assertEqual(response.status_code, 403)


class LDAPUtilsTests(SimpleTestCase):
    @patch("users.ldap_utils.Connection")
    @patch("users.ldap_utils.Server")
    def test_get_ad_users_excludes_inactive_accounts(self, mock_server, mock_connection) -> None:
        mock_conn = Mock()
        mock_conn.extend.standard.paged_search.return_value = [
            {
                "type": "searchResEntry",
                "attributes": {
                    "mail": "active@example.com",
                    "sAMAccountName": "active.user",
                    "company": "CBI",
                    "department": "BI",
                    "name": "Active User",
                    "title": "Engineer",
                    "extensionAttribute1": "A100",
                    "memberOf": ["CN=Analytics,OU=Groups,DC=test,DC=local"],
                    "userAccountControl": "512",
                },
            },
            {
                "type": "searchResEntry",
                "attributes": {
                    "mail": "inactive@example.com",
                    "sAMAccountName": "inactive.user",
                    "company": "CBI",
                    "department": "BI",
                    "name": "Inactive User",
                    "title": "Engineer",
                    "extensionAttribute1": "I100",
                    "memberOf": ["CN=Analytics,OU=Groups,DC=test,DC=local"],
                    "userAccountControl": "514",
                },
            },
        ]
        mock_connection.return_value = mock_conn

        result = get_ad_users("svc_username", "svc_password")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["sAMAccountName"], "active.user")

        paged_search_kwargs = mock_conn.extend.standard.paged_search.call_args.kwargs
        self.assertEqual(
            paged_search_kwargs["search_filter"],
            "(&(objectCategory=person)(objectClass=user)"
            "(!(userAccountControl:1.2.840.113556.1.4.803:=2)))",
        )

    @override_settings(
        LDAP_SERVER_NAME="ldap-primary.example.com",
        LDAP_PORT=389,
        LDAP_USE_SSL=False,
        LDAP_ENABLE_PORT_FALLBACK=False,
        LDAP_SERVER_ALTERNATES=["ldap-secondary.example.com"],
    )
    @patch("users.ldap_utils.Connection")
    @patch("users.ldap_utils.Server")
    def test_get_ad_users_retries_on_socket_error_with_alternate_host(
        self, mock_server, mock_connection
    ) -> None:
        first_error = LDAPSocketOpenError("connection refused")
        successful_conn = Mock()
        successful_conn.extend.standard.paged_search.return_value = []

        mock_connection.side_effect = [first_error, successful_conn]

        result = get_ad_users("svc_username", "svc_password")

        self.assertEqual(result, [])
        self.assertEqual(mock_connection.call_count, 2)

        first_server_call = mock_server.call_args_list[0]
        second_server_call = mock_server.call_args_list[1]

        self.assertEqual(first_server_call.args[0], "ldap-primary.example.com")
        self.assertEqual(second_server_call.args[0], "ldap-secondary.example.com")
