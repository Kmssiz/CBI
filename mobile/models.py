from django.db import models


class ReportMobileLayout(models.Model):
    """Phone layout extracted from a report's .pbix (see mobile/mobile_layout.py)."""

    report = models.OneToOneField('powerbi_report.ReportRef', on_delete=models.CASCADE,
                                  related_name='mobile_layout')
    data = models.JSONField(default=dict)
    # PBIRS ModifiedDate of the file the layout was read from.
    source_modified = models.CharField(max_length=64, blank=True, default='')
    format_version = models.PositiveSmallIntegerField(default=1)
    checked_at = models.DateTimeField()

    def __str__(self):
        return f'Mobile layout of report {self.report_id}'
