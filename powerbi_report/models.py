# powerbi_report/models.py

from django.db import models
from users.models import CustomUser

# Unused models removed for cleanup

class PBIRSServer(models.Model):
    name = models.CharField(max_length=255, unique=True, help_text="A display name for this PBIRS instance")
    base_url = models.URLField(max_length=512, help_text="Base URL for the PBIRS API (e.g., http://server/Reports/api/v2.0)")
    admin_username = models.CharField(max_length=255, help_text="Admin username (e.g., DOMAIN\\User)")
    admin_password = models.CharField(max_length=255, help_text="Admin password")
    is_active = models.BooleanField(default=True, help_text="Whether this server is currently active")

    class Meta:
        verbose_name = "PBIRS Server"
        verbose_name_plural = "PBIRS Servers"
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.base_url})"
# ==================================================================================
# Custom Virtual Folder Structure Models
# ==================================================================================

REPORT_TYPE_ANOMALIE = 'anomalie'
REPORT_TYPE_BIBLIOTHEQUE = 'bibliotheque'
REPORT_TYPE_DASHBOARD = 'dashboard'

REPORT_TYPE_CHOICES = [
    (REPORT_TYPE_ANOMALIE, 'Anomalie'),
    (REPORT_TYPE_BIBLIOTHEQUE, 'BibliothÃ¨que'),
    (REPORT_TYPE_DASHBOARD, 'Dashboard'),
]


class ReportRef(models.Model):
    """
    Local cache of PBIRS report metadata.
    Used to link reports to the custom folder structure without constant API calls.
    """
    pbirs_id = models.CharField(
        max_length=255, 
        unique=True, 
        help_text="Report ID from PBIRS (UUID)"
    )
    name = models.CharField(max_length=255)
    path = models.CharField(max_length=1024, help_text="Path on PBIRS server (e.g., /Sales/Q1)")
    server_url = models.URLField(
        max_length=512,
        blank=True,
        null=True,
        help_text="Base URL of the PBIRS server this report belongs to",
    )
    embed_url = models.URLField(max_length=2048, blank=True, null=True)
    description = models.TextField(blank=True, null=True, help_text="Report description from PBIRS")
    last_synced = models.DateTimeField(auto_now=True)
    modified_at = models.DateTimeField(null=True, blank=True)
    modified_by = models.ForeignKey(
        CustomUser, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='modified_reports'
    )

    # Local metadata fields (not synced from PBIRS)
    pole = models.CharField(
        max_length=128, blank=True, null=True,
        help_text="PÃ´le organisationnel du rapport"
    )
    direction = models.CharField(
        max_length=128, blank=True, null=True,
        help_text="Direction concernÃ©e par le rapport"
    )
    societe = models.CharField(
        max_length=128, blank=True, null=True,
        help_text="SociÃ©tÃ© concernÃ©e par le rapport"
    )
    report_type = models.CharField(
        max_length=20,
        choices=REPORT_TYPE_CHOICES,
        blank=True,
        null=True,
        help_text="Type de rapport : Anomalie, BibliothÃ¨que ou Dashboard"
    )
    is_consolide = models.BooleanField(
        default=False,
        help_text="Rapport consolidÃ© (multi-sociÃ©tÃ©s/pÃ´les â€” champs pÃ´le/sociÃ©tÃ©/direction non applicables)"
    )

    class Meta:
        verbose_name = "Report Reference"
        verbose_name_plural = "Report References"
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.path})"

    @classmethod
    def get_server_url(cls, report_id: str) -> str:
        """Resolve the PBIRS server URL for a given report ID.
        Falls back to the primary server URL if not found.
        """
        from powerbi_report.services.pbirs_servers import get_primary_pbirs_server_url
        try:
            ref = cls.objects.get(pbirs_id=report_id)
            if ref.server_url:
                return ref.server_url
        except cls.DoesNotExist:
            pass
        return get_primary_pbirs_server_url()


class CustomFolder(models.Model):
    """
    A custom folder organization as an alternative to the PBIRS hierarchy.
    view_type determines the top-level view: 'direction', 'department', 'anomalie', 'biblio', 'consolide'.
    Parent can be null for root-level folders.
    """
    VIEW_TYPE_CHOICES = [
        ('direction', 'Direction Folders'),
        ('pole', 'PÃ´le Folders'),
        ('biblio', 'BibliothÃ¨que'),
        ('anomalie', 'Anomalie'),
        ('consolide', 'ConsolidÃ©'),
    ]
    
    name = models.CharField(max_length=255)
    view_type = models.CharField(
        max_length=20, 
        choices=VIEW_TYPE_CHOICES,
        help_text="Which custom view this folder belongs to"
    )
    parent = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='children'
    )
    order = models.PositiveIntegerField(default=0, help_text="Display order within parent")
    created_by = models.ForeignKey(
        CustomUser, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='created_folders'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Custom Folder"
        verbose_name_plural = "Custom Folders"
        ordering = ['order', 'name']

    def __str__(self):
        if self.parent:
            return f"{self.parent} / {self.name}"
        return self.name

    def get_breadcrumbs(self):
        """Returns list of ancestors for navigation."""
        breadcrumbs = []
        folder = self
        while folder:
            breadcrumbs.insert(0, folder)
            folder = folder.parent
        return breadcrumbs
    
    def get_all_report_ids(self):
        """Get all report pbirs_ids in this folder and subfolders."""
        report_ids = set(self.report_items.values_list('report__pbirs_id', flat=True))
        for child in self.children.all():
            report_ids.update(child.get_all_report_ids())
        return report_ids


class FolderReportItem(models.Model):
    """
    Links a ReportRef to a CustomFolder.
    Allows organizing PBIRS reports into the custom structure.
    A report can appear in multiple folders.
    """
    folder = models.ForeignKey(
        CustomFolder,
        on_delete=models.CASCADE,
        related_name='report_items'
    )
    report = models.ForeignKey(
        ReportRef,
        on_delete=models.CASCADE,
        related_name='folder_assignments'
    )
    order = models.PositiveIntegerField(default=0, help_text="Display order within folder")

    class Meta:
        verbose_name = "Folder Report Item"
        verbose_name_plural = "Folder Report Items"
        ordering = ['order', 'report__name']
        unique_together = [['folder', 'report']]

    def __str__(self):
        try:
            return f"{self.report.name} in {self.folder.name}"
        except Exception:
            return f"FolderReportItem (id={self.pk})"


class UserReportPermission(models.Model):
    """
    Local cache of PBIRS report permissions per user.
    Synced from PBIRS server by admin action or automatically after admin changes.
    """
    user = models.ForeignKey(
        CustomUser,
        on_delete=models.CASCADE,
        related_name='report_permissions'
    )
    report = models.ForeignKey(
        ReportRef,
        on_delete=models.CASCADE,
        related_name='user_permissions'
    )
    synced_at = models.DateTimeField(auto_now=True)
    is_direct = models.BooleanField(default=True, help_text="True if permission is assigned directly to user, False if via Group")


    class Meta:
        verbose_name = "User Report Permission"
        verbose_name_plural = "User Report Permissions"
        unique_together = ['user', 'report']
        indexes = [
            models.Index(fields=['user']),
            models.Index(fields=['report']),
        ]

    def __str__(self):
        try:
            return f"{self.user.username} -> {self.report.name}"
        except Exception:
            return f"{self.user.username} -> (Deleted Report)"


class PermissionSyncLog(models.Model):
    """
    Tracks when permission syncs occurred and their status.
    """
    SYNC_STATUS_CHOICES = [
        ('started', 'Started'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    
    started_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=SYNC_STATUS_CHOICES, default='started')
    triggered_by = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='triggered_syncs'
    )
    users_synced = models.PositiveIntegerField(default=0)
    permissions_created = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True, null=True)

    class Meta:
        verbose_name = "Permission Sync Log"
        verbose_name_plural = "Permission Sync Logs"
        ordering = ['-started_at']

    def __str__(self):
        return f"Sync at {self.started_at} - {self.status}"


class MetadataOption(models.Model):
    OPTION_TYPE_CHOICES = [
        ('pole', 'Pôle'),
        ('direction', 'Direction'),
        ('societe', 'Société'),
    ]
    option_type = models.CharField(max_length=20, choices=OPTION_TYPE_CHOICES)
    name = models.CharField(max_length=128)
    
    parent = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='children',
        help_text="Parent option (e.g. link Société to a Pôle)"
    )
    
    class Meta:
        verbose_name = "Metadata Option"
        verbose_name_plural = "Metadata Options"
        unique_together = [['option_type', 'name']]
        ordering = ['option_type', 'name']

    def __str__(self):
        return f"{self.get_option_type_display()} - {self.name}"
