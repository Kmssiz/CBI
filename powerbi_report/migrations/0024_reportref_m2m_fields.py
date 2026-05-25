# Generated manually on 2026-05-24

import django.db.models.deletion
from django.db import migrations, models


def migrate_legacy_metadata_to_m2m(apps, schema_editor):
    ReportRef = apps.get_model('powerbi_report', 'ReportRef')
    MetadataOption = apps.get_model('powerbi_report', 'MetadataOption')
    
    for report in ReportRef.objects.all():
        # Migrate pole (split by comma)
        if report.pole:
            pole_names = [p.strip() for p in report.pole.split(',') if p.strip()]
            for name in pole_names:
                option, _ = MetadataOption.objects.get_or_create(option_type='pole', name=name)
                report.poles.add(option)
                
        # Migrate direction (split by comma)
        if report.direction:
            direction_names = [d.strip() for d in report.direction.split(',') if d.strip()]
            for name in direction_names:
                option, _ = MetadataOption.objects.get_or_create(option_type='direction', name=name)
                report.directions.add(option)
                
        # Migrate societe (split by comma)
        if report.societe:
            societe_names = [s.strip() for s in report.societe.split(',') if s.strip()]
            for name in societe_names:
                option, _ = MetadataOption.objects.get_or_create(option_type='societe', name=name)
                report.societes.add(option)


def reverse_legacy_metadata(apps, schema_editor):
    # Removing relations is handled by field deletion in reverse migration.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('powerbi_report', '0023_metadataoption_parent'),
    ]

    operations = [
        migrations.AddField(
            model_name='reportref',
            name='poles',
            field=models.ManyToManyField(blank=True, help_text='Pôles organisationnels du rapport', limit_choices_to={'option_type': 'pole'}, related_name='pole_reports', to='powerbi_report.metadataoption'),
        ),
        migrations.AddField(
            model_name='reportref',
            name='directions',
            field=models.ManyToManyField(blank=True, help_text='Directions concernées par le rapport', limit_choices_to={'option_type': 'direction'}, related_name='direction_reports', to='powerbi_report.metadataoption'),
        ),
        migrations.AddField(
            model_name='reportref',
            name='societes',
            field=models.ManyToManyField(blank=True, help_text='Sociétés concernées par le rapport', limit_choices_to={'option_type': 'societe'}, related_name='societe_reports', to='powerbi_report.metadataoption'),
        ),
        migrations.AddField(
            model_name='reportref',
            name='modules',
            field=models.ManyToManyField(blank=True, help_text='Modules concernés par le rapport', limit_choices_to={'option_type': 'module'}, related_name='module_reports', to='powerbi_report.metadataoption'),
        ),
        migrations.AlterField(
            model_name='customfolder',
            name='view_type',
            field=models.CharField(choices=[('direction', 'Direction Folders'), ('pole', 'Pôle Folders'), ('biblio', 'Bibliothèque'), ('anomalie', 'Anomalie'), ('consolide', 'Consolidé'), ('module', 'Module')], help_text='Which custom view this folder belongs to', max_length=20),
        ),
        migrations.AlterField(
            model_name='metadataoption',
            name='option_type',
            field=models.CharField(choices=[('pole', 'Pôle'), ('direction', 'Direction'), ('societe', 'Société'), ('module', 'Module')], max_length=20),
        ),
        migrations.RunPython(migrate_legacy_metadata_to_m2m, reverse_legacy_metadata),
    ]
