from django.db import migrations

def backfill_can_view_module(apps, schema_editor):
    CustomUser = apps.get_model("users", "CustomUser")
    CustomUser.objects.filter(can_view_module=False).update(can_view_module=True)

def noop_reverse(apps, schema_editor):
    pass

class Migration(migrations.Migration):

    dependencies = [
        ("users", "0019_customuser_can_view_module_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill_can_view_module, noop_reverse),
    ]
