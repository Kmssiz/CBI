from django.contrib import admin
from .models import ReportRef, CustomFolder, FolderReportItem


class FolderReportItemInline(admin.TabularInline):
    """Inline for adding reports directly when editing a folder."""
    model = FolderReportItem
    extra = 1
    autocomplete_fields = ['report']


@admin.register(ReportRef)
class ReportRefAdmin(admin.ModelAdmin):
    list_display = ['name', 'path', 'pbirs_id', 'last_synced']
    search_fields = ['name', 'path', 'pbirs_id']
    list_filter = ['last_synced']
    readonly_fields = ['last_synced']


@admin.register(CustomFolder)
class CustomFolderAdmin(admin.ModelAdmin):
    list_display = ['name', 'view_type', 'parent', 'order', 'created_by']
    list_filter = ['view_type', 'parent']
    search_fields = ['name']
    inlines = [FolderReportItemInline]
    ordering = ['view_type', 'parent__name', 'order', 'name']
    
    def save_model(self, request, obj, form, change):
        if not change:  # Only set created_by on creation
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(FolderReportItem)
class FolderReportItemAdmin(admin.ModelAdmin):
    list_display = ['report', 'folder', 'order']
    list_filter = ['folder', 'folder__view_type']
    autocomplete_fields = ['report', 'folder']
    search_fields = ['report__name', 'folder__name']
