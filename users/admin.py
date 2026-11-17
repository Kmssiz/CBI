from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserCreationForm, UserChangeForm
from django import forms
from .models import AdminAuditLog, CustomUser, UserHistory, Role


class CustomUserCreationForm(UserCreationForm):
    """Custom form that makes password optional"""
    password1 = forms.CharField(
        label="Password",
        widget=forms.PasswordInput,
        required=False
    )
    password2 = forms.CharField(
        label="Password confirmation",
        widget=forms.PasswordInput,
        required=False
    )

    class Meta:
        model = CustomUser
        fields = ('username', 'email')

    def clean_password2(self):
        password1 = self.cleaned_data.get("password1")
        password2 = self.cleaned_data.get("password2")
        if password1 and password2 and password1 != password2:
            raise forms.ValidationError("Passwords don't match")
        return password2

    def save(self, commit=True):
        user = super().save(commit=False)
        password = self.cleaned_data.get("password1")
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()  # No password needed for LDAP users
        if commit:
            user.save()
        return user


class CustomUserChangeForm(UserChangeForm):
    """Custom form for editing users"""
    password = None  # Remove password field from edit form

    class Meta:
        model = CustomUser
        fields = '__all__'


@admin.register(CustomUser)
class CustomUserAdmin(UserAdmin):
    add_form = CustomUserCreationForm
    form = CustomUserChangeForm
    model = CustomUser
    
    list_display = ('username', 'email', 'first_name', 'last_name', 'role', 'ad2000', 'status', 'is_superuser')
    list_filter = ('role', 'status', 'is_superuser', 'is_active')
    search_fields = ('username', 'email', 'first_name', 'last_name', 'ad2000')
    ordering = ('username',)
    
    fieldsets = (
        (None, {'fields': ('username', 'email')}),
        ('Personal Info', {'fields': ('first_name', 'last_name', 'profile_image')}),
        ('LDAP Info', {'fields': ('ad2000',)}),
        ('Role & Permissions', {'fields': ('role', 'status', 'is_active', 'is_staff', 'is_superuser', 'user_permissions')}),
        ('Important dates', {'fields': ('last_login', 'date_joined')}),
    )
    
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('username', 'email', 'password1', 'password2', 'role', 'ad2000'),
        }),
    )


admin.site.register(UserHistory)
admin.site.register(Role)


@admin.register(AdminAuditLog)
class AdminAuditLogAdmin(admin.ModelAdmin):
    """Read-only trail of administrator actions: nobody can add, edit or delete an entry here."""
    list_display = ('timestamp', 'admin_username', 'method', 'path', 'status_code', 'ip_address')
    list_filter = ('method', 'status_code')
    search_fields = ('admin_username', 'path', 'view_name')
    date_hierarchy = 'timestamp'

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
