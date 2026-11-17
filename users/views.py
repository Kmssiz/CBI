import logging

import requests
from users.utils import person_label
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Permission
from django.core.cache import cache
from powerbi_report.pagination import paginate
from django.http import JsonResponse
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from .directory_sync import deactivate_missing_users, sync_directory_users
from .ldap_utils import connexion_ad2000, get_ad_users
from .session_secrets import store_ldap_password
from .utils import (
    admin_required, apply_role, clean_directory_value, find_directory_user,
    get_user_permissions, is_admin_role, log_history, other_active_admins,
)
from powerbi_report.services.pbirs_servers import get_active_pbirs_server_urls
from powerbi_report.services import sync_user_permissions_on_login
from notifications.models import Notification
from users.models import CustomUser, Role, UserHistory


logger = logging.getLogger("users")


def _normalize_ad_groups(raw_groups):
    """Normalize LDAP group payload into a clean list of strings."""
    if not raw_groups:
        return []
    if not isinstance(raw_groups, list):
        raw_groups = [raw_groups]
    return [str(group).strip() for group in raw_groups if str(group).strip()]

def _post_login_redirect(request):
    """Redirect to the safe ?next= target (e.g. a shared report link), else home."""
    next_url = request.POST.get('next') or request.GET.get('next')
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return redirect(next_url)
    return redirect('home')

#################################################################################################################
#                    Handles user login with LDAP authentication                                                #
#################################################################################################################
def login_view(request):
    if request.user.is_authenticated:
        return _post_login_redirect(request)

    if request.method == 'POST':
        login_identifier = request.POST.get('username')  # Could be email, ad2000, or username
        password = request.POST.get('password')

        # Use new LDAP utility
        user_info = connexion_ad2000(login_identifier, password)
        
        if user_info:
            # Get the canonical username from LDAP (sAMAccountName)
            ldap_username = user_info.get("username", login_identifier)
            email = user_info.get("email", "")
            ad2000 = user_info.get("ad2000", "")
            
            logger.debug(
                "LDAP returned username=%s email=%s ad2000=%s",
                ldap_username,
                email,
                ad2000,
            )

            # Normalize empty-like values ("[]" is how LDAP prints a missing attribute)
            email = clean_directory_value(email)
            ad2000 = clean_directory_value(ad2000) or None  # None for unique constraint compatibility
            
            # Look up user by LDAP username (canonical identifier), then by email, then by ad2000
            logger.debug(
                "Looking up user ldap_username=%s email=%s ad2000=%s",
                ldap_username,
                email,
                ad2000,
            )
            
            # Username first; e-mail / AD2000 only when they point at one single non-admin account.
            user = find_directory_user(ldap_username, email, ad2000 or "")
            logger.debug("Directory lookup result user_id=%s", user.id if user else None)

            if user:
                # Update user info from LDAP (but keep username as LDAP's sAMAccountName)
                # Only update username if it's different (to preserve case if desired, or sync to LDAP)
                if user.username.lower() != ldap_username.lower():
                    user.username = ldap_username
                    
                user.first_name = user_info.get("first_name", "")
                user.last_name = user_info.get("last_name", "")
                user.email = email
                user.ad2000 = ad2000
                # The login lookup does not return AD groups (only the directory sync does): never wipe them here.
                if "ad_groups" in user_info:
                    user.ad_groups = user_info["ad_groups"]
                user.status = "Active"
                # Ensure default view permissions are initialized
                if not hasattr(user, 'can_view_direction'):
                    user.can_view_direction = False
                if not hasattr(user, 'can_view_pole'):
                    user.can_view_pole = False
                    
                logger.debug(
                    "Updated existing user id=%s with ldap_username=%s",
                    user.id,
                    ldap_username,
                )
            else:
                # Create new user
                role, _ = Role.objects.get_or_create(name=settings.USER_ROLE_NAME)
                
                user = CustomUser(
                    username=ldap_username,  # Use LDAP's canonical username
                    first_name=user_info.get("first_name", ""),
                    last_name=user_info.get("last_name", ""),
                    email=email,
                    ad2000=ad2000,
                    ad_groups=user_info.get("ad_groups", []),
                    role=role,
                    status="Active",
                    can_view_direction=False
                )
                user.save()
                new_permissions = role.permissions.all()
                user.user_permissions.add(*new_permissions)

            if not user.is_active:
                messages.error(request, "Ce compte est désactivé. Veuillez contacter le support BI.")
                return render(request, 'users/login.html')

            # CRITICAL: Do NOT save password to DB (user.ldap_password)
            user.backend = 'django.contrib.auth.backends.ModelBackend'
            user.save()

            # Save info to session
            request.session['userinfo'] = user_info
            store_ldap_password(request, password)  # Kept (encrypted) for NTLM
            
            login(request, user)
            log_history(user, "Utilisateur connecté via compte de service PBIRS")
            
            # Normal navigation reads local ReportRef/UserReportPermission cache.
            # Keep login fast; use manual/admin-triggered/hourly PBIRS sync instead.
            if getattr(settings, "PBIRS_SYNC_ON_LOGIN", False):
                try:
                    permissions_synced = sync_user_permissions_on_login(user, password)
                    logger.info(
                        "Synchronisation de %s permissions de rapports pour l'utilisateur %s",
                        permissions_synced,
                        user.username,
                    )
                except Exception as e:
                    logger.error(
                        "Échec de la synchronisation des permissions pour %s: %s",
                        user.username,
                        e,
                    )
            
            # Clear cached data to ensure fresh fetch for this user
            cache.delete_many([
                f"powerbi_reports_cache_{user.id}_PowerBIReports",
                f"powerbi_reports_cache_{user.id}_CatalogItems",
                f"dashboard_data_{user.id}",
            ])
            logger.debug("Cleared login cache for user id=%s", user.id)

            return _post_login_redirect(request)
        else:
            # Fallback: Try local Django authentication (for admin/test users not in LDAP)
            from django.contrib.auth import authenticate
            user = authenticate(request, username=login_identifier, password=password)
            
            if user is not None:
                login(request, user)
                log_history(user, "Utilisateur connecté (Local/Admin)")
                
                # Setup session defaults that might be expected
                request.session['userinfo'] = {
                    "fname": user.first_name,
                    "name": user.last_name,
                    "mail": user.email,
                    "ad2000": user.ad2000
                }

                return _post_login_redirect(request)
            
            messages.error(request, "Identifiants invalides ou authentification échouée.")
    
    return render(request, 'users/login.html')
    
#################################################################################################################
#                    Displays user history for a specific user                                                  #
#################################################################################################################
@admin_required
def user_history(request, user_id=None):

    # Base query
    if user_id:
        history_query = UserHistory.objects.filter(user_id=user_id).select_related('user').order_by('-timestamp')
        selected_user = get_object_or_404(CustomUser, id=user_id)
    else:
        history_query = UserHistory.objects.all().select_related('user').order_by('-timestamp')
        selected_user = None
    
    # Search filtering
    search_query = request.GET.get('search', '').strip()
    if search_query:
        from django.db.models import Q
        history_query = history_query.filter(
            Q(user__username__icontains=search_query) |
            Q(user__first_name__icontains=search_query) |
            Q(user__last_name__icontains=search_query) |
            Q(action__icontains=search_query)
        )

    # Action filtering
    action_filter = request.GET.get('action', '').strip()
    if action_filter:
        history_query = history_query.filter(action__icontains=action_filter)

    # Defined general action categories for dropdown
    actions = [
        "Utilisateur connecté", 
        "Utilisateur déconnecté", 
        "Rôle mis à jour", 
        "Vue par défaut mise à jour"
    ]

    # Pagination
    history_page, page_range = paginate(request, history_query)
        
    permissions = get_user_permissions(request.user)

    return render(request, 'users/user_history.html',
     { 
     'history': history_page,
     'page_range': page_range,
     'permissions': permissions,
     'search_query': search_query,
     'actions': actions,
     'selected_action': action_filter,
     'selected_user': selected_user
     })

#################################################################################################################
#                    Clears user history for a specific user                                                    #
#################################################################################################################
@admin_required
def clear_history(request, user_id):
    
    if request.method == "POST":
        UserHistory.objects.filter(user_id=user_id).delete()  
    return redirect('user_history')

#################################################################################################################
#                    Landing page – shown to every user right after a successful login                          #
#################################################################################################################
@login_required
def landing_page(request):
    """Landing page shown to all users immediately after login."""
    permissions = get_user_permissions(request.user)
    greeting = 'Bonsoir' if timezone.localtime().hour >= 18 else 'Bonjour'
    return render(request, 'landing.html', {
        'permissions': permissions,
        'greeting': greeting,
    })


@login_required
def home_view(request):
    """
    Contrôleur de trafic – redirige chaque utilisateur authentifié vers la page d'accueil (landing).
    """
    return redirect('landing')

#################################################################################################################
#                    Retrieves a dictionary of user permissions                                                 #
#################################################################################################################


#################################################################################################################
#                             Manages user and their roles                                                      #
#################################################################################################################
@admin_required
def user_management(request):
    
    
    # Start with all users
    users_list = CustomUser.objects.select_related('role').order_by('id')
    
    # Server-side search filtering
    search_query = request.GET.get('search', '').strip()
    role_filter = request.GET.get('role', '').strip()
    status_filter = request.GET.get('status', '').strip()
    direction_filter = request.GET.get('direction', '').strip()
    
    if search_query:
        from django.db.models import Q
        users_list = users_list.filter(
            Q(username__icontains=search_query) |
            Q(first_name__icontains=search_query) |
            Q(last_name__icontains=search_query) |
            Q(email__icontains=search_query) |
            Q(ad2000__icontains=search_query)
        )
    
    if role_filter:
        users_list = users_list.filter(role__name__iexact=role_filter)
    
    if status_filter:
        users_list = users_list.filter(status__iexact=status_filter)
    
    if direction_filter:
        users_list = users_list.filter(direction__iexact=direction_filter)
    
    # Get distinct directions for the filter dropdown
    directions = CustomUser.objects.exclude(
        direction__isnull=True
    ).exclude(
        direction=''
    ).values_list('direction', flat=True).distinct().order_by('direction')
    
    # Pagination
    users, page_range = paginate(request, users_list)
    
    roles = Role.objects.all()
    # log_history(request.user, "Page de gestion des utilisateurs consultée")

    permissions = get_user_permissions(request.user)

    from powerbi_report.models import NewUserAlert
    new_users_count = NewUserAlert.objects.filter(seen_at__isnull=True).count()

    return render(request, 'users/user_management.html', {
        'new_users_count': new_users_count,
        'users': users,
        'page_range': page_range,
        'roles': roles,
        'directions': directions,
        'permissions': permissions,
        'search_query': search_query,
        'role_filter': role_filter,
        'status_filter': status_filter,
        'direction_filter': direction_filter,
        'admin_role_name': settings.ADMIN_ROLE_NAME,
    })

#################################################################################################################
#                    Manages roles and their associated permissions                                             #
#################################################################################################################
@admin_required
def manage_roles(request):
    
    roles = Role.objects.all()
    all_permissions = Permission.objects.all()  
    users = CustomUser.objects.all()
    permissions = get_user_permissions(request.user)

    if request.method == "POST":
        user_id = request.POST.get("user_id")
        action = request.POST.get("action")
        user = get_object_or_404(CustomUser, id=user_id)

        new_role = None
        if action == "edit_role":
            new_role = get_object_or_404(Role, id=request.POST.get("new_role"))
        if action in ("remove_role", "edit_role"):
            # Never leave the application without an administrator (yourself included).
            if user.is_admin and not is_admin_role(new_role) and not other_active_admins(user).exists():
                messages.error(request, "Impossible : il doit rester au moins un administrateur actif.")
                return redirect("manage_roles")
            apply_role(user, new_role)
            user.save()
            if user.pk == request.user.pk and not user.is_admin:
                messages.info(request, "Vous n'êtes plus administrateur.")

        return redirect("manage_roles")

    return render(request, 'users/manage_roles.html', {
        "roles": roles,
        "all_permissions": all_permissions, 
        "users": users,
        'permissions': permissions,  
    })

#################################################################################################################
#                    Creates a new role with specified permissions                                              #
#################################################################################################################
@admin_required
def create_role(request):
    
    if request.method == "POST":
        role_name = request.POST.get("role_name", "").strip()
        role_description = request.POST.get("role_description", "").strip()
        selected_permissions = request.POST.getlist("permissions")

        if role_name:
            role, created = Role.objects.get_or_create(name=role_name, defaults={'description': role_description})

            if created:
                if selected_permissions:
                    role.permissions.set(Permission.objects.filter(id__in=selected_permissions))
                messages.success(request, "Rôle créé avec succès.")
            else:
                messages.warning(request, "Ce rôle existe déjà.")

            return redirect("manage_roles")

        messages.error(request, "Le nom du rôle ne peut pas être vide.")

    return render(request, "users/manage_roles.html")

#################################################################################################################
#                    Edits an existing role's name and description                                              #
#################################################################################################################
@admin_required
def edit_role(request, role_id):
    
    role = get_object_or_404(Role, id=role_id)
    all_permissions = Permission.objects.all()

    if request.method == "POST":
        new_role_name = request.POST.get("role_name", "").strip()
        new_role_description = request.POST.get("role_description", "").strip()

        if not new_role_name:
            messages.error(request, "Le nom du rôle ne peut pas être vide.")
        elif Role.objects.filter(name=new_role_name).exclude(id=role_id).exists():
            messages.warning(request, "Un rôle avec ce nom existe déjà.")
        else:
            role.name = new_role_name
            role.description = new_role_description
            role.save()
            messages.success(request, "Rôle mis à jour avec succès.")
        return redirect("manage_roles")

    return render(request, "users/manage_roles.html", {
        "role": role,
        "all_permissions": all_permissions,
        "role_permissions": role.permissions.all(),
    })

#################################################################################################################
#                    Removes a role if not assigned to any users                                                #
#################################################################################################################
@admin_required
def remove_role(request, role_id):
    
    role = get_object_or_404(Role, id=role_id)

    if CustomUser.objects.filter(role=role).exists():
        messages.error(request, "Impossible de supprimer un rôle assigné à des utilisateurs.")
        return redirect("manage_roles")

    role.delete()
    messages.success(request, "Rôle supprimé avec succès.")
    return redirect("manage_roles")

#################################################################################################################
#                    Manages permissions for a specific role                                                    #
#################################################################################################################
@admin_required
def permissions_list(request, role_id):
    
    role = get_object_or_404(Role, id=role_id)
    permissions_list = Permission.objects.select_related('content_type')
    all_permissions = role.permissions.select_related('content_type')

    if request.method == "POST":
        action = request.POST.get("action")
        permission_id = request.POST.get("permission")

        if action == "grant" and permission_id:
            permission = get_object_or_404(Permission, id=permission_id)
            role.permissions.add(permission)
            users_with_role = CustomUser.objects.filter(role=role)
            for user in users_with_role:
                user.user_permissions.add(permission)
            messages.success(request, f"Permission '{permission.name}' ajoutée au rôle '{role.name}' et aux utilisateurs associés.")
        elif action == "revoke" and permission_id:
            
            permission = get_object_or_404(Permission, id=permission_id)
            role.permissions.remove(permission)
            users_with_role = CustomUser.objects.filter(role=role)
            for user in users_with_role:
                user.user_permissions.remove(permission)
            messages.success(request, f"Permission '{permission.name}' supprimée du rôle '{role.name}' et des utilisateurs associés.")

        elif action == "grant_all":
            permissions_list=Permission.objects.all()
            role.permissions.set(permissions_list)
            users_with_role = CustomUser.objects.filter(role=role)
            for user in users_with_role:
                user.user_permissions.set(all_permissions)
            messages.success(request, f"Toutes les permissions ont été ajoutées au rôle '{role.name}' et aux utilisateurs associés.")

        elif action == "revoke_all":
            role.permissions.clear()
            users_with_role = CustomUser.objects.filter(role=role)
            for user in users_with_role:
                user.user_permissions.clear()
            messages.success(request, f"Toutes les permissions ont été supprimées du rôle '{role.name}' et des utilisateurs associés.")

        return redirect("permissions_list", role_id=role_id)

    permissions = get_user_permissions(request.user)
    granted_ids = set(role.permissions.values_list('id', flat=True))  # one query, not one per row
    return render(request, 'users/permissions_list.html', {
        'role': role,
        'granted_ids': granted_ids,
        'all_permissions': all_permissions,
        'permissions_list':permissions_list,
        'permissions': permissions,  
    })

#################################################################################################################
#                    Synchronizes users with LDAP directory                                                     #
#################################################################################################################
@admin_required
@require_POST
def sync_users(request):
    """
    Synchronizes users with LDAP directory.
    Optimized for large datasets using bulk lookups to avoid O(N) database queries.
    """
    # Use default LDAP service account for user synchronization
    ldap_username = settings.LDAP_SERVICE_USERNAME
    ldap_password = settings.LDAP_SERVICE_PASSWORD
    
    # Validate that service credentials are configured
    if not ldap_username or not ldap_password:
        messages.error(request, "Compte de service LDAP non configuré. Veuillez définir LDAP_SERVICE_USERNAME et LDAP_SERVICE_PASSWORD dans le fichier .env.")
        return redirect('users_view')

    ldap_users = get_ad_users(ldap_username, ldap_password)
    
    if not ldap_users:
         messages.error(request, "Échec de la récupération des utilisateurs LDAP ou aucun utilisateur trouvé.")
         return redirect('users_view')

    result = sync_directory_users(ldap_users)
    count, already_exist = result.created, result.existing
    if getattr(settings, 'LDAP_DEACTIVATE_MISSING', False):
        switched_off = deactivate_missing_users(ldap_users)
        if switched_off:
            messages.warning(request, f"{switched_off} compte(s) désactivé(s) dans l'annuaire ont été désactivés ici aussi.")

    messages.success(request, f"Synchronisation des utilisateurs terminée. {count} nouveaux utilisateurs ajoutés. ({already_exist} existaient déjà)")
    return redirect('users_view')






#################################################################################################################
#                   Displays details for a specific user or the current user                                    #
#################################################################################################################
@login_required
def user_details(request):
    user_id = request.GET.get('user_id')
    permissions = get_user_permissions(request.user)

    if user_id:
        detail_user = get_object_or_404(CustomUser, id=user_id)
        # Security check: Only admins or the user themselves can view details
        is_admin = request.user.is_superuser or (request.user.role and request.user.role.name.lower() == settings.ADMIN_ROLE_NAME.lower())
        if not is_admin and detail_user != request.user:
            raise PermissionDenied
    else:
        detail_user = request.user
    
    
    # What Odoo last told us about this employee (nothing when they are not an active Odoo employee).
    from powerbi_report.models import OdooProfile
    odoo_profile = OdooProfile.objects.filter(user=detail_user).first()

    return render(request, 'users/user_details.html', {
        'odoo_profile': odoo_profile,
        'user_detail': detail_user,
        'permissions': permissions,  
    })

#################################################################################################################
#                   Edits a user's role and permissions, setting superuser status for admin role                #
#################################################################################################################
@admin_required
def user_edit(request, user_id):

    user = get_object_or_404(CustomUser, id=user_id)
    if request.method == 'POST':
        new_role_id = request.POST.get('role')  # Get the selected role ID
        new_role = get_object_or_404(Role, id=new_role_id) if new_role_id else None  # Fetch the role object
        becomes_admin = is_admin_role(user.role)

        if new_role and user.is_admin and not is_admin_role(new_role) and not other_active_admins(user).exists():
            messages.error(request, "Impossible : il doit rester au moins un administrateur actif.")
            return redirect("user_edit", user_id=user.id)

        if new_role:
            # Remove all existing permissions
            user.user_permissions.clear()

            # Assign new permissions based on the new role
            new_permissions = new_role.permissions.all()  # Assuming Role model has a 'permissions' ManyToMany field
            user.user_permissions.add(*new_permissions)  # Assign the new role's permissions

            # Update role and superuser status
            apply_role(user, new_role)
            becomes_admin = is_admin_role(new_role)

        if becomes_admin:
            # Admin has unrestricted access to all sections
            user.can_view_direction = True
            user.can_view_pole = True
            user.can_view_anomalie = True
            user.can_view_consolide = True
            user.can_view_module = True
            user.can_use_slider = True
        else:
            # Update specific permissions independently from the checkboxes
            user.can_view_direction = request.POST.get('can_view_direction') == 'on'
            user.can_view_pole = request.POST.get('can_view_pole') == 'on'
            user.can_view_anomalie = request.POST.get('can_view_anomalie') == 'on'
            user.can_view_consolide = request.POST.get('can_view_consolide') == 'on'
            user.can_view_module = request.POST.get('can_view_module') == 'on'
            user.can_use_slider = request.POST.get('can_use_slider') == 'on'
        
        user.save()

        # Log the updates
        if new_role:
            log_history(request.user, f"Rôle mis à jour pour {user.username} vers {new_role.name}")

        # Notify the user
        if new_role:
            Notification.objects.create(
                user=user,
                message=f"Votre rôle a été mis à jour : {new_role.name}."
            )

        # Notify all admins
        admins = CustomUser.objects.filter(role__name__iexact=settings.ADMIN_ROLE_NAME)
        for admin in admins:
            if admin != request.user:
                Notification.objects.create(
                    user=admin,
                    message=f"{person_label(request.user)} a modifié les paramètres de {person_label(user)}."
                )

        messages.success(request, "Paramètres utilisateur mis à jour avec succès.")

        return redirect('users_view')

    return redirect("users_view")

#################################################################################################################
#                    Logs out the user and clears session data                                                  #
#################################################################################################################
@login_required
@require_POST
def logout_view(request):
    user_id = request.user.id
    request.user.status = "Not Active"
    request.user.save()
    log_history(request.user, "Utilisateur déconnecté")
    request.session.flush()
    logout(request)
    cache.delete_many([
        f"powerbi_reports_cache_{user_id}_PowerBIReports",
        f"powerbi_reports_cache_{user_id}_CatalogItems",
        f"dashboard_data_{user_id}",
    ])

    return redirect("login")

def server_status(request):
    """
    Connectivity of the configured PowerBI Report Servers, for the login page's 'Système' pop-up.

    Public on purpose (nobody is logged in there): anonymous callers only learn the overall status.
    Server addresses and error details are reserved for logged-in users.
    """
    detailed = request.user.is_authenticated
    try:
        server_urls = get_active_pbirs_server_urls()
        if not server_urls:
            return JsonResponse({"status": "down", **({"error": "Aucun serveur PBIRS actif configuré"} if detailed else {})}, status=500)

        results = []
        all_up = True
        for url in server_urls:
            try:
                response = requests.get(url, timeout=5)
                if response.status_code < 500:
                    results.append({"url": url, "status": "up"})
                else:
                    results.append({"url": url, "status": "down", "code": response.status_code})
                    all_up = False
            except requests.exceptions.RequestException as e:
                results.append({"url": url, "status": "down", "error": str(e)})
                all_up = False

        overall_status = "up" if all_up else ("partial" if any(r["status"] == "up" for r in results) else "down")
        payload = {"status": overall_status}
        if detailed:
            payload["servers"] = results
        return JsonResponse(payload)
    except Exception as e:
        logger.warning("Échec de la vérification du statut PBIRS: %s", e)
        return JsonResponse({"status": "down", **({"error": str(e)} if detailed else {})}, status=500)


#################################################################################################################
#          New users: people who exist in the directory AND in Odoo, waiting for an admin to look at them       #
#################################################################################################################
def _new_user_row(alert):
    from django.core.exceptions import ObjectDoesNotExist
    user = alert.user
    try:
        profile = user.odoo_profile
    except ObjectDoesNotExist:
        profile = None
    return {
        'alert': alert,
        'user': user,
        'title': profile.title if profile else '',
        'company': (profile.company if profile and profile.company else user.societe) or '',
        'phone': profile.phone if profile else '',
        'direction': user.direction or '',
    }


@admin_required
def new_users(request):
    from datetime import timedelta
    from powerbi_report.models import NewUserAlert

    alerts = NewUserAlert.objects.select_related('user', 'user__odoo_profile')
    waiting = [_new_user_row(a) for a in alerts.filter(seen_at__isnull=True)]
    recent = [_new_user_row(a) for a in
              alerts.filter(seen_at__gte=timezone.now() - timedelta(days=14)).order_by('-seen_at')[:50]]

    return render(request, 'users/new_users.html', {
        'waiting': waiting,
        'recent': recent,
        'permissions': get_user_permissions(request.user),
    })


@admin_required
def new_user_seen(request):
    """Mark some (or all) of the waiting new users as seen."""
    from powerbi_report.models import NewUserAlert

    if request.method != 'POST':
        return redirect('new_users')
    waiting = NewUserAlert.objects.filter(seen_at__isnull=True)
    if request.POST.get('all') != '1':
        ids = [int(i) for i in request.POST.getlist('alerts') if i.isdigit()]
        waiting = waiting.filter(id__in=ids)
    marked = waiting.update(seen_at=timezone.now(), seen_by=request.user)
    messages.success(request, f"{marked} personne{'s' if marked > 1 else ''} marquée{'s' if marked > 1 else ''} comme vue{'s' if marked > 1 else ''}.")
    return redirect('new_users')
