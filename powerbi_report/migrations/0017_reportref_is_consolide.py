from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('powerbi_report', '0016_reportref_metadata_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='reportref',
            name='is_consolide',
            field=models.BooleanField(
                default=False,
                help_text='Rapport consolidé (multi-sociétés/pôles — champs pôle/société/direction non applicables)',
            ),
        ),
    ]
