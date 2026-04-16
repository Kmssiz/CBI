from django.core.management.base import BaseCommand, CommandError

from powerbi_report.services import sync_all_user_permissions, sync_report_refs


class Command(BaseCommand):
    help = "Sync PBIRS report metadata and permissions into the local database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reports-only",
            action="store_true",
            help="Only sync ReportRef metadata, not user permissions.",
        )

    def handle(self, *args, **options):
        try:
            if options["reports_only"]:
                synced_count, removed_count = sync_report_refs()
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Synced {synced_count} PBIRS reports; removed {removed_count} stale local rows."
                    )
                )
                return

            users_synced, permissions_count = sync_all_user_permissions()
            self.stdout.write(
                self.style.SUCCESS(
                    f"Synced permissions for {users_synced} users; created {permissions_count} rows."
                )
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc
