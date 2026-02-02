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
            
            messages.error(request, "Invalid credentials or authentication failed.")
    
    return render(request, 'users/login.html')
    
#################################################################################################################
#                    Displays user history for a specific user                                                  #
#################################################################################################################
@login_required
def user_history(request):
    if not request.user.role or request.user.role.name != "admin":
        messages.error(request, "Permission denied. Admin access required.")
        return redirect('home')

    history = UserHistory.objects.all().select_related('user').order_by('-timestamp')
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread = Notification.objects.filter(user=request.user, is_read=False).count()
    permissions = get_user_permissions(request.user)

    return render(request, 'users/user_history.html',
     { 
     'history': history ,
     'notifications': notifications, 
     'unread': unread,
     'permissions': permissions,
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
        log_history(request.user, "Viewed home page")
        return redirect('powerbi_report:dashboard')
    
    if request.user.role and request.user.role.name == "user":
        # Check default view preference
        if hasattr(request.user, 'default_view'):
            if request.user.default_view == 'business':
                log_history(request.user, "Viewed Business View (Home)")
                return redirect('powerbi_report:custom_business')
            elif request.user.default_view == 'department':
                log_history(request.user, "Viewed Department View (Home)")
                return redirect('powerbi_report:custom_department')
        
        # Fallback
        log_history(request.user, "Viewed report_list_hierarchy page")
        return redirect('powerbi_report:report_list_hierarchy')
    
    return redirect('profile')

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
    users = CustomUser.objects.all()
    roles = Role.objects.all()
    log_history(request.user, "Accessed user management page")

    permissions = get_user_permissions(request.user)

    return render(request, 'users/user_management.html', {
        'notifications': notifications,
        'unread': unread_count,
        'users': users,
        'roles': roles,
        'permissions': permissions,  
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

    current_password = request.session.get('ldap_password')
    if not current_password:
         messages.error(request, "Session expired or password not found. Please login again.")
         return redirect('users_view')

    ldap_users = get_ad_users(request.user.username, current_password)
    
    if not ldap_users:
         messages.error(request, "Failed to fetch LDAP users or no users found.")
         return redirect('users_view')

    count = 0
    for ldap_user in ldap_users:
        ad2000 = ldap_user.get("ad2000", "").strip()
        # Fallback to creating ad2000 from samaccountname if missing, or skip
        if not ad2000:
             continue 

        user = CustomUser.objects.filter(ad2000__iexact=ad2000).first()
        if not user:
            user_role, _ = Role.objects.get_or_create(name="user")

            user = CustomUser(
                username=ldap_user.get("sAMAccountName"),  
                ad2000=ad2000,
                first_name=ldap_user.get("name", "").split(' ')[0], # Rough approx
                last_name=" ".join(ldap_user.get("name", "").split(' ')[1:]),
                email=ldap_user.get("mail", "").strip(),
                role=user_role,
                status="Not Active"
            )
            user.save()  
            user.user_permissions.set(user_role.permissions.all())  
            count += 1

    messages.success(request, f"User synchronization completed. {count} new users added.")
    return redirect('users_view')

#################################################################################################################
#                    Displays the user's profile page                                                           #
#################################################################################################################
@login_required
def profile(request):
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    unread_count = Notification.objects.filter(user=request.user, is_read=False).count()
    
    log_history(request.user, "Viewed profile")
    permissions = get_user_permissions(request.user)

    context = {
        'user': request.user,
        'notifications': notifications,
        'unread': unread_count,
        'permissions': permissions,  
    }
    return render(request, 'users/profile.html', context)

#################################################################################################################
#                    Handles profile image updates for the user                                                 #
#################################################################################################################
@login_required
def edit_profile(request):
    if request.method == "POST":
        if 'profile_image' in request.FILES:
            request.user.profile_image = request.FILES['profile_image']
            request.user.save()
        return redirect('profile')
    else:
        return redirect('profile')

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