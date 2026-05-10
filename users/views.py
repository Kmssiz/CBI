import logging

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render

from .ldap_utils import connexion_ad2000, get_ad_users
from .utils import log_history, get_user_permissions, admin_required
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

#################################################################################################################
#                    Handles user login with LDAP authentication                                                #
#################################################################################################################
def login_view(request):
    if request.user.is_authenticated:
        return redirect('home')
        
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

            # Normalize empty-like values
            email = email.strip() if email else ""
            ad2000 = ad2000.strip() if ad2000 else ""
            if ad2000 == "[]" or ad2000 == "":
                ad2000 = None  # Use None for unique constraint compatibility
            
            # Look up user by LDAP username (canonical identifier), then by email, then by ad2000
            logger.debug(
                "Looking up user ldap_username=%s email=%s ad2000=%s",
                ldap_username,
                email,
                ad2000,
            )
            
            # Use case-insensitive lookup for username
            user = CustomUser.objects.filter(username__iexact=ldap_username).first()
            logger.debug("Username lookup result user_id=%s", user.id if user else None)
            
            if not user and email:
                user = CustomUser.objects.filter(email__iexact=email).first()
                logger.debug("Email lookup result user_id=%s", user.id if user else None)
            
            if not user and ad2000:
                user = CustomUser.objects.filter(ad2000__iexact=ad2000).first()
                logger.debug("AD2000 lookup result user_id=%s", user.id if user else None)

            if user:
                # Update user info from LDAP (but keep username as LDAP's sAMAccountName)
                # Only update username if it's different (to preserve case if desired, or sync to LDAP)
                if user.username.lower() != ldap_username.lower():
                    user.username = ldap_username
                    
                user.first_name = user_info.get("first_name", "")
                user.last_name = user_info.get("last_name", "")
                user.email = email
                user.ad2000 = ad2000
                user.ad_groups = user_info.get("ad_groups", [])
                user.status = "Active"
                # Ensure default view is valid (repair legacy/invalid values).
                if user.default_view not in {"direction", "pole"}:
                    user.default_view = "direction"
                    user.can_view_direction = True
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
                    default_view="direction",
                    can_view_direction=True
                )
                user.save()
                new_permissions = role.permissions.all()
                user.user_permissions.add(*new_permissions)

            # CRITICAL: Do NOT save password to DB (user.ldap_password)
            user.backend = 'django.contrib.auth.backends.ModelBackend'
            user.save()

            # Save info to session
            request.session['userinfo'] = user_info
            request.session['ldap_password'] = password # Kept for NTLM
            
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
            cache.delete(f"powerbi_reports_cache_{user.id}")
            cache.delete(f"dashboard_data_{user.id}")
            logger.debug("Cleared login cache for user id=%s", user.id)

            return redirect('home')
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
                
                return redirect('home')
            
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
    paginator = Paginator(history_query, 10)  # 10 items per page
    page_number = request.GET.get('page')
    history_page = paginator.get_page(page_number)
    
    # Calculate page range for pagination UI
    try:
        page_range = paginator.get_elided_page_range(history_page.number, on_each_side=2, on_ends=1)
    except:
        page_range = []
        
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = Notification.objects.filter(user=request.user, is_read=False).count()
    permissions = get_user_permissions(request.user)

    return render(request, 'users/user_history.html',
     { 
     'history': history_page,
     'page_range': page_range,
     'notifications': notifications, 
     'unread': unread,
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
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = notifications.filter(is_read=False).count()
    permissions = get_user_permissions(request.user)
    return render(request, 'landing.html', {
        'notifications': notifications,
        'unread': unread,
        'permissions': permissions,
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
    
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread_count = notifications.filter(is_read=False).count()
    
    # Start with all users
    users_list = CustomUser.objects.all().order_by('id')
    
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
    paginator = Paginator(users_list, 10)  # Show 10 users per page
    page_number = request.GET.get('page')
    users = paginator.get_page(page_number)
    
    # Calculate page range for pagination UI
    try:
        page_range = paginator.get_elided_page_range(users.number, on_each_side=2, on_ends=1)
    except:
        page_range = []
    
    roles = Role.objects.all()
    # log_history(request.user, "Page de gestion des utilisateurs consultée")

    permissions = get_user_permissions(request.user)

    return render(request, 'users/user_management.html', {
        'notifications': notifications,
        'unread': unread_count,
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
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = Notification.objects.filter(user=request.user, is_read=False).count()
    permissions = get_user_permissions(request.user)

    if request.method == "POST":
        user_id = request.POST.get("user_id")
        action = request.POST.get("action")
        user = CustomUser.objects.get(id=user_id)

        if action == "remove_role":
            user.role = None
            user.save()
        elif action == "edit_role":
            new_role_id = request.POST.get("new_role")
            new_role = Role.objects.get(id=new_role_id)
            user.role = new_role
            user.save()

        return redirect("manage_roles")

    return render(request, 'users/manage_roles.html', {
        "roles": roles,
        "all_permissions": all_permissions, 
        "users": users,
        'notifications': notifications,
        'unread': unread,
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
    permissions_list=Permission.objects.all()
    all_permissions = role.permissions.all()
    
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

    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread_count = Notification.objects.filter(user=request.user, is_read=False).count()
    permissions = get_user_permissions(request.user)
    return render(request, 'users/permissions_list.html', {
        'role': role,
        'all_permissions': all_permissions,
        'permissions_list':permissions_list,
        'notifications': notifications,
        'unread': unread_count,
        'permissions': permissions,  
    })

#################################################################################################################
#                    Synchronizes users with LDAP directory                                                     #
#################################################################################################################
@admin_required
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

    # Optimization: Fetch all existing users in one query to create local lookup maps
    # We only need a few fields for identification and change detection
    all_db_users = CustomUser.objects.all().only('id', 'username', 'ad2000', 'pole', 'direction', 'societe', 'ad_groups')
    username_map = {u.username.lower(): u for u in all_db_users}
    ad2000_map = {u.ad2000.lower(): u for u in all_db_users if u.ad2000}

    count = 0
    already_exist = 0
    skipped = 0
    
    user_role, _ = Role.objects.get_or_create(name=settings.USER_ROLE_NAME)

    for ldap_user in ldap_users:
        ad2000 = (ldap_user.get("ad2000") or "").strip()
        sam_account = (ldap_user.get("sAMAccountName") or "").strip()
        ldap_groups = _normalize_ad_groups(ldap_user.get("ad_groups", []))
        
        # Fallback to sAMAccountName if ad2000 (extensionAttribute1) is empty or just "[]"
        if not ad2000 or ad2000 == "[]":
            ad2000 = sam_account
        
        # Skip if still no identifier available
        if not ad2000:
            skipped += 1
            continue

        # Check lookup maps first (O(1))
        user = ad2000_map.get(ad2000.lower())
        if not user:
            user = username_map.get(sam_account.lower())

        if not user:
            try:
                user = CustomUser(
                    username=sam_account,  
                    ad2000=ad2000,
                    societe=ldap_user.get("company", "").strip(),
                    pole=ldap_user.get("company", "").strip(),
                    direction=ldap_user.get("department", "").strip(),
                    first_name=ldap_user.get("name", "").split(' ')[0],
                    last_name=" ".join(ldap_user.get("name", "").split(' ')[1:]),
                    email=ldap_user.get("mail", "").strip(),
                    ad_groups=ldap_groups,
                    role=user_role,
                    status="Not Active",
                    default_view="direction",
                    can_view_direction=True
                )
                user.save()  
                user.user_permissions.set(user_role.permissions.all())  
                count += 1
                
                # Update maps for subsequent lookups in the same loop
                username_map[user.username.lower()] = user
                if user.ad2000:
                    ad2000_map[user.ad2000.lower()] = user
            except Exception as e:
                logger.exception("Échec de l'enregistrement de l'utilisateur LDAP synchronisé %s: %s", sam_account, e)
        else:
            # Efficiently update existing users only if fields changed
            update_fields = {}
            company = ldap_user.get("company", "").strip()
            department = ldap_user.get("department", "").strip()
            
            if not user.pole and company:
                user.pole = company
                update_fields["pole"] = company
            if not user.direction and department:
                user.direction = department
                update_fields["direction"] = department
            if not user.societe and company:
                user.societe = company
                update_fields["societe"] = company
            
            # Ensure default view is valid (repair legacy/invalid values).
            if user.default_view not in {"direction", "pole"}:
                user.default_view = "direction"
                user.can_view_direction = True
                user.can_view_pole = False
                update_fields["default_view"] = "direction"
                update_fields["can_view_direction"] = True
                update_fields["can_view_pole"] = False

            if ldap_groups:
                existing_groups = _normalize_ad_groups(user.ad_groups)
                if set(existing_groups) != set(ldap_groups):
                    update_fields["ad_groups"] = ldap_groups

            if update_fields:
                CustomUser.objects.filter(pk=user.pk).update(**update_fields)
            
            already_exist += 1

    logger.info(
        "Résumé de la synchronisation LDAP total=%s créés=%s existants=%s ignorés=%s",
        len(ldap_users),
        count,
        already_exist,
        skipped,
    )
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
    
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = Notification.objects.filter(user=request.user, is_read=False).count()
    
    return render(request, 'users/user_details.html', {
        'user_detail': detail_user,
        'notifications': notifications,
        'unread': unread,
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
        new_default_view = request.POST.get('default_view', 'direction')  # Get the default view

        if new_role:
            # Remove all existing permissions
            user.user_permissions.clear()

            # Assign new permissions based on the new role
            new_permissions = new_role.permissions.all()  # Assuming Role model has a 'permissions' ManyToMany field
            user.user_permissions.add(*new_permissions)  # Assign the new role's permissions

            # Update role and superuser status
            user.role = new_role  
            is_admin_role = new_role.name.lower() == settings.ADMIN_ROLE_NAME
            user.is_superuser = is_admin_role
            user.is_staff = is_admin_role
        
        # Update default view
        old_view = user.default_view
        user.default_view = new_default_view

        if is_admin_role:
            # Admin has unrestricted access to all sections
            user.can_view_direction = True
            user.can_view_pole = True
            user.can_view_anomalie = True
            user.can_view_consolide = True
        else:
            # Sync boolean permission fields with the default view
            user.can_view_direction = (new_default_view == 'direction')
            user.can_view_pole = (new_default_view == 'pole')

            # Update specific permissions
            user.can_view_anomalie = request.POST.get('can_view_anomalie') == 'on'
            user.can_view_consolide = request.POST.get('can_view_consolide') == 'on'
        
        user.save()

        # Log the updates
        if new_role:
            log_history(request.user, f"Rôle mis à jour pour {user.username} vers {new_role.name}")
        if old_view != new_default_view:
            view_label = 'Vue Direction' if new_default_view == 'direction' else 'Vue Pôle'
            log_history(request.user, f"Vue par défaut mise à jour pour {user.username} vers {view_label}")

        # Notify the user
        if new_role:
            Notification.objects.create(
                user=user,
                message=f"Votre rôle a été mis à jour : {new_role.name}."
            )
        if old_view != new_default_view:
            view_label = 'Vue Direction' if new_default_view == 'direction' else 'Vue Pôle'
            Notification.objects.create(
                user=user,
                message=f"Votre vue par défaut a été modifiée : {view_label}."
            )

        # Notify all admins
        admins = CustomUser.objects.filter(role__name__iexact=settings.ADMIN_ROLE_NAME)
        for admin in admins:
            if admin != request.user:
                Notification.objects.create(
                    user=admin,
                    message=f"{request.user.username} a modifié les paramètres de {user.username}."
                )

        messages.success(request, "Paramètres utilisateur mis à jour avec succès.")

        return redirect('users_view')

    return redirect("users_view")

#################################################################################################################
#                    Logs out the user and clears session data                                                  #
#################################################################################################################
@login_required
def logout_view(request):
    user_id = request.user.id
    request.user.status = "Not Active"
    request.user.save()
    log_history(request.user, "Utilisateur déconnecté")
    request.session.flush()
    logout(request)
    cache.delete(f"powerbi_reports_cache_{user_id}")
    cache.delete(f"dashboard_data_{user_id}")

    return redirect("login")

@login_required
def server_status(request):
    """
    Endpoint to check connectivity to all configured PowerBI Report Servers.
    Used by the login page 'Systeme Status' feature.
    """
    try:
        server_urls = get_active_pbirs_server_urls()
        if not server_urls:
            return JsonResponse({"status": "down", "error": "Aucun serveur PBIRS actif configuré"}, status=500)

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
        return JsonResponse({"status": overall_status, "servers": results})
    except Exception as e:        
        logger.warning("Échec de la vérification du statut PBIRS: %s", e)
        return JsonResponse({"status": "down", "error": str(e)}, status=500)

