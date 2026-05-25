# Generated manually on 2026-05-24

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0018_alter_customuser_can_view_direction'),
    ]

    operations = [
        migrations.AddField(
            model_name='customuser',
            name='can_view_module',
            field=models.BooleanField(default=True, help_text='Accès à la vue Module'),
        ),
        migrations.RemoveField(
            model_name='customuser',
            name='default_view',
        ),
    ]
