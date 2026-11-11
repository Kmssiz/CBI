from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('powerbi_report', '0034_job_rules_odoo_profile'),
    ]

    operations = [
        migrations.AddField(
            model_name='odooprofile',
            name='phone',
            field=models.CharField(blank=True, default='', max_length=60),
        ),
    ]
