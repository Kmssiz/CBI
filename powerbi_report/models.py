# powerbi_report/models.py

from django.db import models
from django.contrib.auth.models import User
from users.models import CustomUser

# Unused models removed for cleanup



# ==================================================================================
# Custom Virtual Folder Structure Models
# ==================================================================================

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
    embed_url = models.URLField(max_length=2048, blank=True, null=True)
    last_synced = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Report Reference"
        verbose_name_plural = "Report References"
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.path})"


class CustomFolder(models.Model):
    """
    Virtual folder node supporting View A (business) and View B (department/role).
    Parent can be null for root-level folders.
    """
    VIEW_TYPE_CHOICES = [
        ('business', 'Business Folders'),
        ('department', 'Department/Role'),
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
        return f"{self.report.name} in {self.folder.name}"
