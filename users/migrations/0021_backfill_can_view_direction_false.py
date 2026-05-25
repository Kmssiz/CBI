from django.db import migrations
from django.conf import settings

def backfill_can_view_direction_false(apps, schema_editor):
    CustomUser = apps.get_model("users", "CustomUser")
    admin_role_name = getattr(settings, 'ADMIN_ROLE_NAME', 'admin')
    
    # Update all non-admin users to have can_view_direction = False
    CustomUser.objects.exclude(is_superuser=True).exclude(role__name__iexact=admin_role_name).update(can_view_direction=False)

def noop_reverse(apps, schema_editor):
    pass

class Migration(migrations.Migration):

    dependencies = [
        ("users", "0020_backfill_can_view_module"),
    ]

    operations = [
        migrations.RunPython(backfill_can_view_direction_false, noop_reverse),
    ]
