import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


def keep_the_cutoff(apps, schema_editor):
    """People already in the app when jobs were introduced are not 'new': keep that date."""
    JobSettings = apps.get_model('powerbi_report', 'JobSettings')
    OdooSettings = apps.get_model('powerbi_report', 'OdooSettings')
    old = JobSettings.objects.first()
    OdooSettings.objects.get_or_create(
        pk=1, defaults={'new_users_since': old.baseline_at if old else timezone.now()},
    )


class Migration(migrations.Migration):
    """
    The jobs (métiers) feature is removed: nobody gets access automatically any more.
    New users are announced to the admins instead, who decide what to grant by hand.
    Access already granted in PBIRS is untouched; only the app's own bookkeeping goes.
    """

    dependencies = [
        ('powerbi_report', '0035_odooprofile_phone'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='OdooSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('new_users_since', models.DateTimeField()),
            ],
        ),
        migrations.CreateModel(
            name='NewUserAlert',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('detected_at', models.DateTimeField(auto_now_add=True)),
                ('seen_at', models.DateTimeField(blank=True, null=True)),
                ('seen_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='new_user_alert', to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['-detected_at']},
        ),
        migrations.RunPython(keep_the_cutoff, migrations.RunPython.noop),
        migrations.DeleteModel(name='JobGrant'),
        migrations.DeleteModel(name='JobMembership'),
        migrations.DeleteModel(name='JobRule'),
        migrations.DeleteModel(name='JobGroup'),
        migrations.DeleteModel(name='JobSettings'),
        migrations.DeleteModel(name='OdooJobSeen'),
    ]
