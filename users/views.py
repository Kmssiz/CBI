import requests
from requests.auth import HTTPBasicAuth
from django.shortcuts import render, redirect, get_object_or_404 ,HttpResponse
from django.urls import reverse_lazy
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login, logout
from notifications.models import Notification
from users.models import CustomUser,UserHistory,Role
from django.utils.timezone import now
from django.core.cache import cache
from django.conf import settings  
from django.contrib.auth.models import Permission
from guardian.shortcuts import assign_perm, remove_perm
from django.contrib.auth.decorators import login_required, permission_required
from django.shortcuts import render, redirect
from django.contrib.auth.models import Permission, Group
from django.core.paginator import Paginator

from .ldap_utils import connexion_ad2000, get_ad_users
from .utils import log_history, get_user_permissions

#################################################################################################################
#                    Handles user login with LDAP authentication                                                #
#################################################################################################################
'''
def login_view(request):
    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')

        response = requests.post(LDAP_API_URL, auth=HTTPBasicAuth(username, password))
        
        if response.status_code == 200:
            data = response.json()
            if data.get("authenticated"):
                userinfo = data.get("userinfo", {})
                email = userinfo.get("mail", "")
                ad2000 = userinfo.get("ad2000", "")

                user = (CustomUser.objects.filter(username=username).first() or
                        CustomUser.objects.filter(email=email).first() or
                        CustomUser.objects.filter(ad2000=ad2000).first())

                if user:
                    user.username = user.username or username
                    user.first_name = user.first_name or userinfo.get("fname", "")
                    user.last_name = user.last_name or userinfo.get("name", "")
                    user.ad2000 = user.ad2000 or ad2000
                    user.status = "Active"

                else:
                    user_role, created = Role.objects.get_or_create(name="user")
                    user = CustomUser(
                        username=username,
                        first_name=userinfo.get("fname", ""),
                        last_name=userinfo.get("name", ""),
                        email=email,
                        ad2000=ad2000,
                        role=user_role,
                        status="Active"
                    )
                    user.save()

                    new_permissions = user_role.permissions.all()
                    user.user_permissions.add(*new_permissions)
                
                user.ldap_password = password  
                user.backend = 'django.contrib.auth.backends.ModelBackend'
                user.save()

                request.session['userinfo'] = userinfo
                request.session['ldap_password'] = password  
                user.ldap_password = password  

                user.backend = 'django.contrib.auth.backends.ModelBackend'
                user.save()
                login(request, user)
                log_history(user, "User logged in")

                if user.role and user.role.name == "admin":
                    return redirect('powerbi_report:dashboard')
                return redirect('home')
            else:
                messages.error(request, "Invalid credentials")
        else:
            messages.error(request, "Authentication failed.")
    
    return render(request, 'users/login.html')
'''
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
            
            print(f"[DEBUG LOGIN] LDAP returned: username='{ldap_username}', email='{email}', ad2000='{ad2000}'")

            # Normalize empty-like values
            email = email.strip() if email else ""
            ad2000 = ad2000.strip() if ad2000 else ""
            if ad2000 == "[]" or ad2000 == "":
                ad2000 = None  # Use None for unique constraint compatibility
            
            # Look up user by LDAP username (canonical identifier), then by email, then by ad2000
            print(f"[DEBUG LOGIN] Looking up user with ldap_username='{ldap_username}', email='{email}', ad2000='{ad2000}'")
            
            # Use case-insensitive lookup for username
            user = CustomUser.objects.filter(username__iexact=ldap_username).first()
            print(f"[DEBUG LOGIN] Username lookup result: {user.id if user else None}")
            
            if not user and email:
                user = CustomUser.objects.filter(email__iexact=email).first()
                print(f"[DEBUG LOGIN] Email lookup result: {user.id if user else None}")
            
            if not user and ad2000:
                user = CustomUser.objects.filter(ad2000__iexact=ad2000).first()
                print(f"[DEBUG LOGIN] AD2000 lookup result: {user.id if user else None}")

            if user:
                # Update user info from LDAP (but keep username as LDAP's sAMAccountName)
                # Only update username if it's different (to preserve case if desired, or sync to LDAP)
                if user.username.lower() != ldap_username.lower():
                    user.username = ldap_username
                    
                user.first_name = user_info.get("first_name", "")
                user.last_name = user_info.get("last_name", "")
                user.email = email
                user.ad2000 = ad2000
                user.status = "Active"
                print(f"[DEBUG LOGIN] Updated existing user {user.id} with ldap_username='{ldap_username}'")
            else:
                # Create new user
                role, created = Role.objects.get_or_create(name="user")
                
                user = CustomUser(
                    username=ldap_username,  # Use LDAP's canonical username
                    first_name=user_info.get("first_name", ""),
                    last_name=user_info.get("last_name", ""),
                    email=email,
                    ad2000=ad2000,
                    role=role,
                    status="Active"
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
            log_history(user, "User logged in")
            
            # Clear cached data to ensure fresh fetch for this user
            cache.delete(f"powerbi_reports_cache_{user.id}")
            cache.delete(f"dashboard_data_{user.id}")
            print(f"[DEBUG LOGIN] Cleared cache for user {user.id}")

            if user.role and user.role.name == "admin":
                return redirect('powerbi_report:dashboard')
            return redirect('home')
        else:
            # Fallback: Try local Django authentication (for admin/test users not in LDAP)
            from django.contrib.auth import authenticate
            user = authenticate(request, username=login_identifier, password=password)
            
            if user is not None:
                login(request, user)
                log_history(user, "User logged in (Local/Admin)")
                
                # Setup session defaults that might be expected
                request.session['userinfo'] = {
                    "fname": user.first_name,
                    "name": user.last_name,
                    "mail": user.email,
                    "ad2000": user.ad2000
                }
                
                if user.role and user.role.name == "admin":
                    return redirect('powerbi_report:dashboard')
                return redirect('home')
            
            messages.error(request, "Identifiants invalides ou authentification échouée.")
    
    return render(request, 'users/login.html')
    
#################################################################################################################
#                    Displays user history for a specific user                                                  #
#################################################################################################################
@login_required
@login_required
def user_history(request):
    if not request.user.role or request.user.role.name != "admin":
        messages.error(request, "Permission denied. Admin access required.")
        return redirect('home')

    # Base query
    history_query = UserHistory.objects.all().select_related('user').order_by('-timestamp')
    
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
        "User logged in", 
        "User logged out", 
        "Updated role", 
        "Updated default view"
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
     'selected_action': action_filter
     })

#################################################################################################################
#                    Clears user history for a specific user                                                    #
#################################################################################################################
@login_required
def clear_history(request, user_id):
    if not request.user.role or request.user.role.name != "admin":
        messages.error(request, "Permission denied. Admin access required.")
        return redirect('home')
    
    if request.method == "POST":
        UserHistory.objects.filter(user_id=user_id).delete()  
    return redirect('user_history')

#################################################################################################################
#                    Redirects users to appropriate home page based on role                                     #
#################################################################################################################
@login_required
def home_view(request):
    if request.user.role and request.user.role.name == "admin":
        # log_history(request.user, "Viewed home page")
        return redirect('powerbi_report:dashboard')
    
    if request.user.role and request.user.role.name == "user":
        # Check default view preference
        if hasattr(request.user, 'default_view'):
            if request.user.default_view == 'business':
                # log_history(request.user, "Viewed Business View (Home)")
                return redirect('powerbi_report:custom_business')
            elif request.user.default_view == 'department':
                # log_history(request.user, "Viewed Department View (Home)")
                return redirect('powerbi_report:custom_department')
        
        # Fallback
        # log_history(request.user, "Viewed report_list_hierarchy page")
        return redirect('powerbi_report:report_list_hierarchy')
    
    return redirect('logout')

#################################################################################################################
#                    Retrieves a dictionary of user permissions                                                 #
#################################################################################################################


#################################################################################################################
#                             Manages user and their roles                                                      #
#################################################################################################################
@login_required
def user_management(request):
    if not request.user.role or request.user.role.name != "admin":
        messages.error(request, "Permission denied. Admin access required.")
        return redirect('home')
    
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread_count = notifications.filter(is_read=False).count()
    
    # Start with all users
    users_list = CustomUser.objects.all().order_by('id')
    
    # Server-side search filtering
    search_query = request.GET.get('search', '').strip()
    role_filter = request.GET.get('role', '').strip()
    status_filter = request.GET.get('status', '').strip()
    societe_filter = request.GET.get('societe', '').strip()
    
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
    
    if societe_filter:
        users_list = users_list.filter(societe__iexact=societe_filter)
    
    # Get distinct sociétés for the filter dropdown
    societes = CustomUser.objects.exclude(
        societe__isnull=True
    ).exclude(
        societe=''
    ).values_list('societe', flat=True).distinct().order_by('societe')
    
    # Pagination
    paginator = Paginator(users_list, 10)  # Show 10 users per page
    page_number = request.GET.get('page')
    users = paginator.get_page(page_number)
    
    roles = Role.objects.all()
    # log_history(request.user, "Accessed user management page")

    permissions = get_user_permissions(request.user)

    return render(request, 'users/user_management.html', {
        'notifications': notifications,
        'unread': unread_count,
        'users': users,
        'roles': roles,
        'societes': societes,
        'permissions': permissions,
        'search_query': search_query,
        'role_filter': role_filter,
        'status_filter': status_filter,
        'societe_filter': societe_filter,
    })

#################################################################################################################
#                    Manages roles and their associated permissions                                             #
#################################################################################################################
@login_required
def manage_roles(request):
    if not request.user.role or request.user.role.name != "admin":
        messages.error(request, "Permission denied. Admin access required.")
        return redirect('home')
    
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
@login_required
def create_role(request):
    if not request.user.role or request.user.role.name != "admin":
        messages.error(request, "Permission denied. Admin access required.")
        return redirect('home')
    
    if request.method == "POST":
        role_name = request.POST.get("role_name", "").strip()
        role_description = request.POST.get("role_description", "").strip()
        selected_permissions = request.POST.getlist("permissions")

        if role_name:
            role, created = Role.objects.get_or_create(name=role_name, defaults={'description': role_description})

            if created:
                if selected_permissions:
                    role.permissions.set(Permission.objects.filter(id__in=selected_permissions))
                messages.success(request, "Role created successfully.")
            else:
                messages.warning(request, "Role already exists.")

            return redirect("manage_roles")

        messages.error(request, "Role name cannot be empty.")

    return render(request, "users/manage_roles.html")

#################################################################################################################
#                    Edits an existing role's name and description                                              #
#################################################################################################################
@login_required
def edit_role(request, role_id):
    if not request.user.role or request.user.role.name != "admin":
        messages.error(request, "Permission denied. Admin access required.")
        return redirect('home')
    
    role = get_object_or_404(Role, id=role_id)
    all_permissions = Permission.objects.all()

    if request.method == "POST":
        new_role_name = request.POST.get("role_name", "").strip()
        new_role_description = request.POST.get("role_description", "").strip()

        if not new_role_name:
            messages.error(request, "Role name cannot be empty.")
        elif Role.objects.filter(name=new_role_name).exclude(id=role_id).exists():
            messages.warning(request, "A role with this name already exists.")
        else:
            role.name = new_role_name
            role.description = new_role_description
            role.save()
            messages.success(request, "Role updated successfully.")
        return redirect("manage_roles")

    return render(request, "users/manage_roles.html", {
        "role": role,
        "all_permissions": all_permissions,
        "role_permissions": role.permissions.all(),
    })

#################################################################################################################
#                    Removes a role if not assigned to any users                                                #
#################################################################################################################
@login_required
def remove_role(request, role_id):
    if not request.user.role or request.user.role.name != "admin":
        messages.error(request, "Permission denied. Admin access required.")
        return redirect('home')
    
    role = get_object_or_404(Role, id=role_id)

    if CustomUser.objects.filter(role=role).exists():
        messages.error(request, "Cannot delete role assigned to users.")
        return redirect("manage_roles")

    role.delete()
    messages.success(request, "Role deleted successfully.")
    return redirect("manage_roles")

#################################################################################################################
#                    Manages permissions for a specific role                                                    #
#################################################################################################################
@login_required
def permissions_list(request, role_id):
    if not request.user.role or request.user.role.name != "admin":
        messages.error(request, "Permission denied. Admin access required.")
        return redirect('home')
    
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
            messages.success(request, f"Permission '{permission.name}' added to role '{role.name}' and associated users.")
        elif action == "revoke" and permission_id:
            
            permission = get_object_or_404(Permission, id=permission_id)
            role.permissions.remove(permission)
            users_with_role = CustomUser.objects.filter(role=role)
            for user in users_with_role:
                user.user_permissions.remove(permission)
            messages.success(request, f"Permission '{permission.name}' removed from role '{role.name}' and associated users.")

        elif action == "grant_all":
            permissions_list=Permission.objects.all()
            role.permissions.set(permissions_list)
            users_with_role = CustomUser.objects.filter(role=role)
            for user in users_with_role:
                user.user_permissions.set(all_permissions)
            messages.success(request, f"All permissions added to role '{role.name}' and associated users.")

        elif action == "revoke_all":
            role.permissions.clear()
            users_with_role = CustomUser.objects.filter(role=role)
            for user in users_with_role:
                user.user_permissions.clear()
            messages.success(request, f"All permissions removed from role '{role.name}' and associated users.")

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
@login_required
def sync_users(request):
    if not request.user.role or request.user.role.name.lower() != "admin":
        messages.error(request, "You do not have permission to perform this action.")
        return redirect('report_list')  

    # Use default LDAP service account for user synchronization
    ldap_username = settings.LDAP_SERVICE_USERNAME
    ldap_password = settings.LDAP_SERVICE_PASSWORD
    
    # Validate that service credentials are configured
    if not ldap_username or not ldap_password:
        messages.error(request, "LDAP service account not configured. Please set LDAP_SERVICE_USERNAME and LDAP_SERVICE_PASSWORD in .env")
        return redirect('users_view')

    ldap_users = get_ad_users(ldap_username, ldap_password)
    
    if not ldap_users:
         messages.error(request, "Failed to fetch LDAP users or no users found.")
         return redirect('users_view')

    count = 0
    skipped = 0
    already_exist = 0
    
    # Debug: print first 5 LDAP users to see actual data
    for i, ldap_user in enumerate(ldap_users[:5]):
        print(f"[SYNC DEBUG] Sample user {i}: sAMAccountName='{ldap_user.get('sAMAccountName')}', ad2000='{ldap_user.get('ad2000')}'")
    
    for ldap_user in ldap_users:
        ad2000 = ldap_user.get("ad2000", "").strip()
        sam_account = ldap_user.get("sAMAccountName", "").strip()
        
        # Fallback to sAMAccountName if ad2000 (extensionAttribute1) is empty or just "[]"
        if not ad2000 or ad2000 == "[]":
            ad2000 = sam_account
        
        # Skip if still no identifier available
        if not ad2000:
            skipped += 1
            continue

        # Check if user already exists by ad2000 OR by username
        user = CustomUser.objects.filter(ad2000__iexact=ad2000).first()
        matched_by = "ad2000" if user else None
        
        if not user:
            user = CustomUser.objects.filter(username__iexact=sam_account).first()
            if user:
                matched_by = "username"
        
        # Debug: Print first few matches to understand why they're matching
        if user and already_exist < 5:
            print(f"[SYNC DEBUG] Match found for LDAP user '{sam_account}' (ad2000='{ad2000}') - matched by {matched_by} to DB user id={user.id}, username='{user.username}', ad2000='{user.ad2000}'")
        
        if not user:
            user_role, _ = Role.objects.get_or_create(name="user")

            try:
                user = CustomUser(
                    username=sam_account,  
                    ad2000=ad2000,
                    societe=ldap_user.get("company", "").strip(),
                    first_name=ldap_user.get("name", "").split(' ')[0],
                    last_name=" ".join(ldap_user.get("name", "").split(' ')[1:]),
                    email=ldap_user.get("mail", "").strip(),
                    role=user_role,
                    status="Not Active"
                )
                user.save()  
                user.user_permissions.set(user_role.permissions.all())  
                count += 1
            except Exception as e:
                print(f"[SYNC DEBUG] Failed to save user {sam_account}: {e}")
        else:
            # Update société for existing users if not set
            if not user.societe and ldap_user.get("company"):
                user.societe = ldap_user.get("company", "").strip()
                user.save(update_fields=['societe'])
            already_exist += 1

    print(f"[SYNC DEBUG] Total LDAP users: {len(ldap_users)}, Created: {count}, Already exist: {already_exist}, Skipped (no ID): {skipped}")
    messages.success(request, f"User synchronization completed. {count} new users added. ({already_exist} already existed)")
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
@login_required
def user_edit(request, user_id):
    user = get_object_or_404(CustomUser, id=user_id)
    roles = Role.objects.all()  # Get all available roles

    if request.method == 'POST':
        new_role_id = request.POST.get('role')  # Get the selected role ID
        new_role = get_object_or_404(Role, id=new_role_id) if new_role_id else None  # Fetch the role object
        new_default_view = request.POST.get('default_view', 'business')  # Get the default view

        if new_role:
            # Remove all existing permissions
            user.user_permissions.clear()

            # Assign new permissions based on the new role
            new_permissions = new_role.permissions.all()  # Assuming Role model has a 'permissions' ManyToMany field
            user.user_permissions.add(*new_permissions)  # Assign the new role's permissions

            # Update role and superuser status
            user.role = new_role  
            user.is_superuser = new_role.name.lower() == 'admin'  # Set superuser status only for admin role
            user.is_staff = new_role.name.lower() == 'admin'  # Optionally set is_staff for admin access
        
        # Update default view (always update, even if role is not changed)
        old_view = user.default_view
        user.default_view = new_default_view
        user.save()

        # Log the updates
        if new_role:
            log_history(request.user, f"Updated role for {user.username} to {new_role.name}")
        if old_view != new_default_view:
            view_label = 'Business View' if new_default_view == 'business' else 'Department View'
            log_history(request.user, f"Updated default view for {user.username} to {view_label}")

        # Notify the user
        if new_role:
            Notification.objects.create(
                user=user,
                message=f"Your role has been updated to {new_role.name}."
            )
        if old_view != new_default_view:
            view_label = 'Business View' if new_default_view == 'business' else 'Department View'
            Notification.objects.create(
                user=user,
                message=f"Your default view has been changed to {view_label}."
            )

        # Notify all admins
        admins = CustomUser.objects.filter(role__name='admin')  
        for admin in admins:
            if admin != request.user:
                Notification.objects.create(
                    user=admin,
                    message=f"{request.user.username} updated settings for {user.username}."
                )

        messages.success(request, "User settings updated successfully.")

        return redirect('users_view')

    return render(request, 'users/user_edit.html', {'user': user, 'roles': roles})

#################################################################################################################
#                    Logs out the user and clears session data                                                  #
#################################################################################################################
@login_required
def logout_view(request):
    user_id = request.user.id
    request.user.status = "Not Active"
    request.user.save()
    log_history(request.user, "User logged out")
    request.session.flush()
    logout(request)
    cache.delete(f"powerbi_reports_cache_{user_id}")
    cache.delete(f"dashboard_data_{user_id}")

    return redirect("login")

from django.http import JsonResponse
from django.conf import settings
import requests

def server_status(request):
    """
    Endpoint to check connectivity to the PowerBI Report Server.
    Used by the login page 'Systeme Status' feature.
    """
    try:
        url = getattr(settings, 'POWERBI_REPORT_SERVER_URL', None)
        if not url:
            return JsonResponse({'status': 'down', 'error': 'Configuration missing'}, status=500)
            
        # Ping the server with a short timeout
        response = requests.get(url, timeout=5)
        if response.status_code < 500:
            return JsonResponse({'status': 'up'})
        else:
            return JsonResponse({'status': 'down', 'code': response.status_code})
    except Exception as e:
        return JsonResponse({'status': 'down', 'error': str(e)}, status=500)
