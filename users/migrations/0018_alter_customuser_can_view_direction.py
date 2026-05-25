# Generated manually on 2026-05-24

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0017_backfill_default_direction_view'),
    ]

    operations = [
        migrations.AlterField(
            model_name='customuser',
            name='can_view_direction',
            field=models.BooleanField(default=False, help_text='Accès à la vue Direction'),
        ),
    ]
