import logging
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.contrib import messages
from django.views.decorators.http import require_http_methods
from .models import PBIRSServer, MetadataOption, ReportRef
from notifications.models import Notification
from users.utils import get_user_permissions, admin_required
from django.conf import settings
import json

logger = logging.getLogger('powerbi_report')


@login_required
@admin_required
def server_management_list(request):
    servers = PBIRSServer.objects.all().order_by('name')
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = notifications.filter(is_read=False).count()
    permissions = get_user_permissions(request.user)

    metadata_options = MetadataOption.objects.all()
    poles = [opt for opt in metadata_options if opt.option_type == 'pole']
    directions = [opt for opt in metadata_options if opt.option_type == 'direction']
    societes = [opt for opt in metadata_options if opt.option_type == 'societe']

    context = {
        'servers': servers,
        'poles': poles,
        'directions': directions,
        'societes': societes,
        'notifications': notifications,
        'unread': unread,
        'permissions': permissions,
    }
    return render(request, 'powerbi_report/server_management.html', context)

@login_required
@admin_required
@require_http_methods(["POST"])
def server_create(request):
    try:
        data = request.POST
        name = data.get('name')
        base_url = data.get('base_url')
        admin_username = data.get('admin_username')
        admin_password = data.get('admin_password')
        is_active = data.get('is_active') == 'on'

        if not name or not base_url or not admin_username or not admin_password:
            messages.error(request, "Tous les champs sont requis.")
            return redirect('powerbi_report:server_management_list')

        PBIRSServer.objects.create(
            name=name,
            base_url=base_url,
            admin_username=admin_username,
            admin_password=admin_password,
            is_active=is_active
        )
        messages.success(request, f"Serveur '{name}' ajouté avec succès.")
    except Exception as e:
        logger.error(f"Error creating server: {e}")
        messages.error(request, f"Erreur lors de l'ajout: {e}")

    return redirect('powerbi_report:server_management_list')

@login_required
@admin_required
@require_http_methods(["POST"])
def server_edit(request, server_id):
    try:
        server = get_object_or_404(PBIRSServer, id=server_id)
        data = request.POST
        
        server.name = data.get('name', server.name)
        server.base_url = data.get('base_url', server.base_url)
        server.admin_username = data.get('admin_username', server.admin_username)
        
        # Only update password if provided
        new_password = data.get('admin_password')
        if new_password:
            server.admin_password = new_password
            
        server.is_active = data.get('is_active') == 'on'
        server.save()
        
        messages.success(request, f"Serveur '{server.name}' modifié avec succès.")
    except Exception as e:
        logger.error(f"Error updating server {server_id}: {e}")
        messages.error(request, f"Erreur lors de la modification: {e}")

    return redirect('powerbi_report:server_management_list')

@login_required
@admin_required
@require_http_methods(["POST"])
def server_delete(request, server_id):
    try:
        server = get_object_or_404(PBIRSServer, id=server_id)
        name = server.name
        server.delete()
        messages.success(request, f"Serveur '{name}' supprimé avec succès.")
    except Exception as e:
        logger.error(f"Error deleting server {server_id}: {e}")
        messages.error(request, f"Erreur lors de la suppression: {e}")

    return redirect('powerbi_report:server_management_list')


@login_required
@admin_required
@require_http_methods(["POST"])
def metadata_option_create(request):
    try:
        data = request.POST
        option_type = data.get('option_type')
        name = data.get('name')
        parent_id = data.get('parent_id')

        if not option_type or not name:
            messages.error(request, "Le type et le nom de l'option sont requis.")
            return redirect('powerbi_report:server_management_list')

        parent = None
        if option_type == 'societe' and parent_id:
            parent = get_object_or_404(MetadataOption, id=parent_id, option_type='pole')

        option, created = MetadataOption.objects.get_or_create(
            option_type=option_type,
            name=name,
            defaults={'parent': parent}
        )
        if not created and parent:
            option.parent = parent
            option.save()

        messages.success(request, f"L'option '{name}' a été ajoutée avec succès.")
    except Exception as e:
        logger.error(f"Error creating metadata option: {e}")
        messages.error(request, f"Erreur lors de l'ajout: {e}")

    return redirect('powerbi_report:server_management_list')

@login_required
@admin_required
@require_http_methods(["POST"])
def metadata_option_edit(request, option_id):
    try:
        option = get_object_or_404(MetadataOption, id=option_id)
        data = request.POST
        name = data.get('name')
        parent_id = data.get('parent_id')

        if not name:
            messages.error(request, "Le nom de l'option est requis.")
            return redirect('powerbi_report:server_management_list')

        old_name = option.name
        
        # Propagate change to ReportRef instances
        if old_name != name:
            if option.option_type == 'pole':
                ReportRef.objects.filter(pole=old_name).update(pole=name)
            elif option.option_type == 'direction':
                ReportRef.objects.filter(direction=old_name).update(direction=name)
            elif option.option_type == 'societe':
                ReportRef.objects.filter(societe=old_name).update(societe=name)

        option.name = name
        
        if option.option_type == 'societe':
            if parent_id:
                parent = get_object_or_404(MetadataOption, id=parent_id, option_type='pole')
                option.parent = parent
            else:
                option.parent = None
        
        option.save()
        messages.success(request, f"L'option '{name}' a été modifiée avec succès.")
    except Exception as e:
        logger.error(f"Error editing metadata option {option_id}: {e}")
        messages.error(request, f"Erreur lors de la modification: {e}")

    return redirect('powerbi_report:server_management_list')

@login_required
@admin_required
@require_http_methods(["POST"])
def metadata_option_delete(request, option_id):
    try:
        option = get_object_or_404(MetadataOption, id=option_id)
        name = option.name
        
        # Propagate delete to ReportRef: set matching fields to None
        if option.option_type == 'pole':
            ReportRef.objects.filter(pole=name).update(pole=None)
        elif option.option_type == 'direction':
            ReportRef.objects.filter(direction=name).update(direction=None)
        elif option.option_type == 'societe':
            ReportRef.objects.filter(societe=name).update(societe=None)
            
        option.delete()
        messages.success(request, f"L'option '{name}' a été supprimée avec succès.")
    except Exception as e:
        logger.error(f"Error deleting metadata option {option_id}: {e}")
        messages.error(request, f"Erreur lors de la suppression: {e}")

    return redirect('powerbi_report:server_management_list')
