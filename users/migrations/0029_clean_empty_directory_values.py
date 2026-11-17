from django.db import migrations


def clean(apps, schema_editor):
    """LDAP prints a missing attribute as "[]": earlier logins stored that text as an e-mail / AD2000."""
    CustomUser = apps.get_model('users', 'CustomUser')
    CustomUser.objects.filter(email='[]').update(email='')
    CustomUser.objects.filter(ad2000='[]').update(ad2000=None)


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0028_admin_audit_log'),
    ]

    operations = [
        migrations.RunPython(clean, migrations.RunPython.noop),
    ]
