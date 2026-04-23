from django.contrib.auth.models import AbstractUser, Group, Permission
from django.db import models

class Role(models.Model):
    name = models.CharField(max_length=50, unique=True)
    permissions = models.ManyToManyField(Permission, blank=True) 
    description = models.CharField(max_length=255,default='no description')
    def __str__(self):
        return self.name

class CustomUser(AbstractUser):
    VIEW_CHOICES = [
        ('direction', 'Direction View'),
        ('pole', 'Pôle View'),
    ]
    
    ad2000 = models.CharField(max_length=255,blank=True, null=True, unique=True,help_text="AD2000 identifier from LDAP")
    direction = models.CharField(max_length=255, blank=True, null=True, help_text="Direction from LDAP")
    pole = models.CharField(max_length=255, blank=True, null=True, help_text="Pôle from LDAP")
    societe = models.CharField(max_length=255, blank=True, null=True, help_text="Company/Société from LDAP")
    role = models.ForeignKey(Role, on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(max_length=20, default='Not Active')
    profile_image = models.ImageField(upload_to='profile_images/', null=True, blank=True)
    user_permissions = models.ManyToManyField(Permission, blank=True)
    can_view_consolide = models.BooleanField(default=False, help_text="Accès à la page des rapports consolidés")
    default_view = models.CharField(
        max_length=20, 
        choices=VIEW_CHOICES, 
        default='direction',
        help_text="Default view for this user (set by admin)"
    ) 
    ad_groups = models.JSONField(default=list, blank=True, help_text="Cached list of AD groups") 
    has_seen_onboarding = models.BooleanField(default=False, help_text="Indicates if the user has completed the onboarding guide")

    def get_initials(self) -> str:
        """Returns user initials ."""
        if self.first_name and self.last_name:
            return f"{self.first_name[0].upper()}.{self.last_name[0].upper()}"
        
        # Fallback to AD2000 first character if available
        if self.ad2000:
            return self.ad2000[:1].upper()
            
        # Fallback if first/last name and AD2000 are not set
        if self.username:
            if len(self.username) >= 2:
                return f"{self.username[0].upper()}.{self.username[1].upper()}"
            return self.username[:1].upper()
        return "?"

    def get_avatar_color(self) -> str:
        """Returns a deterministic hex color based on the username."""
        colors = [
            '#137fec', # Primary Blue
            '#059669', # Emerald
            '#7c3aed', # Violet
            '#db2777', # Pink
            '#d97706', # Amber
            '#2563eb', # Blue
            '#4f46e5', # Indigo
            '#0891b2', # Cyan
            '#be185d', # Rose
            '#c026d3', # Fuchsia
        ]
        import hashlib
        # Use ad2000 if available, otherwise username
        seed = self.ad2000 or self.username or "default"
        hash_val = int(hashlib.md5(seed.encode()).hexdigest(), 16)
        return colors[hash_val % len(colors)]

    @property
    def is_admin(self) -> bool:
        """Checks if the user is a superuser or has the admin role."""
        from django.conf import settings
        return self.is_superuser or (self.role and self.role.name.lower() == settings.ADMIN_ROLE_NAME.lower())

    def __str__(self):
        return self.username
    


class UserHistory(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="history")
    action = models.CharField(max_length=255)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - {self.action} at {self.timestamp}"

