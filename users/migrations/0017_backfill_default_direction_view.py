from django.db import migrations


def backfill_default_direction_view(apps, schema_editor):
    CustomUser = apps.get_model("users", "CustomUser")

    # Legacy rows can have null/blank or pre-rename values (business/department).
    # Normalize them so the default experience remains Direction.
    users_to_fix = CustomUser.objects.exclude(default_view__in=["direction", "pole"])
    users_to_fix.update(
        default_view="direction",
        can_view_direction=True,
        can_view_pole=False,
    )


def noop_reverse(apps, schema_editor):
    # No safe reverse because original invalid values are unknown.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0016_customuser_can_view_anomalie_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_default_direction_view, noop_reverse),
    ]
