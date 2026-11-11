from django.db import migrations


class Migration(migrations.Migration):
    """RLS roles are per report and assigned by hand: no global company → role alias."""

    dependencies = [
        ('powerbi_report', '0032_company_rls_role'),
    ]

    operations = [
        migrations.DeleteModel(name='CompanyRlsRole'),
    ]
