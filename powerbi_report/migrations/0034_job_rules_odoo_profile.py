import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.utils import timezone


def carry_over(apps, schema_editor):
    """
    Title → group mappings made on the Odoo page become rules ("any company").
    Every account that exists today is protected: groups only act on accounts
    created after this moment.
    """
    OdooJobTitle = apps.get_model('powerbi_report', 'OdooJobTitle')
    JobRule = apps.get_model('powerbi_report', 'JobRule')
    JobSettings = apps.get_model('powerbi_report', 'JobSettings')
    OdooJobSeen = apps.get_model('powerbi_report', 'OdooJobSeen')
    for old in OdooJobTitle.objects.all():
        OdooJobSeen.objects.get_or_create(
            title_key=old.title_key, company_key='',
            defaults={'title': old.title, 'employee_count': old.employee_count, 'last_seen': old.last_seen},
        )
        if old.job_group_id:
            JobRule.objects.get_or_create(
                job_group_id=old.job_group_id, title_key=old.title_key, company_key='',
                defaults={'title': old.title, 'company': ''},
            )
    JobSettings.objects.get_or_create(pk=1, defaults={'baseline_at': timezone.now()})


class Migration(migrations.Migration):

    dependencies = [
        ('powerbi_report', '0033_remove_company_rls_role'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='JobSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('baseline_at', models.DateTimeField(help_text='Les comptes créés avant cette date ne sont jamais modifiés par les métiers')),
            ],
        ),
        migrations.CreateModel(
            name='JobRule',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title_key', models.CharField(help_text='Intitulé normalisé (sans accents ni casse)', max_length=200)),
                ('title', models.CharField(help_text="Intitulé tel qu'écrit dans Odoo", max_length=200)),
                ('company_key', models.CharField(blank=True, default='', help_text='Société normalisée ; vide = toutes les sociétés', max_length=200)),
                ('company', models.CharField(blank=True, default='', max_length=200)),
                ('job_group', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='rules', to='powerbi_report.jobgroup')),
            ],
            options={'ordering': ['title', 'company'], 'unique_together': {('job_group', 'title_key', 'company_key')}},
        ),
        migrations.CreateModel(
            name='OdooProfile',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('employee_id', models.CharField(max_length=100)),
                ('title', models.CharField(max_length=200)),
                ('title_key', models.CharField(db_index=True, max_length=200)),
                ('company', models.CharField(blank=True, default='', max_length=200)),
                ('company_key', models.CharField(blank=True, db_index=True, default='', max_length=200)),
                ('synced_at', models.DateTimeField(auto_now=True)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='odoo_profile', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='OdooJobSeen',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title_key', models.CharField(max_length=200)),
                ('title', models.CharField(max_length=200)),
                ('company_key', models.CharField(blank=True, default='', max_length=200)),
                ('company', models.CharField(blank=True, default='', max_length=200)),
                ('employee_count', models.PositiveIntegerField(default=0)),
                ('last_seen', models.DateTimeField(blank=True, null=True)),
            ],
            options={'ordering': ['title', 'company'], 'unique_together': {('title_key', 'company_key')}},
        ),
        migrations.AlterField(
            model_name='jobgroup',
            name='name',
            field=models.CharField(help_text='Nom du groupe', max_length=150, unique=True),
        ),
        migrations.AlterField(
            model_name='jobmembership',
            name='user',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='job_memberships', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='jobmembership',
            name='source',
            field=models.CharField(choices=[('auto', 'Automatique'), ('manual', 'Manuel')], default='manual', max_length=10),
        ),
        migrations.AlterUniqueTogether(
            name='jobmembership',
            unique_together={('user', 'job_group')},
        ),
        migrations.RunPython(carry_over, migrations.RunPython.noop),
        migrations.DeleteModel(name='OdooJobTitle'),
    ]
