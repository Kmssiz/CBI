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

    def __str__(self):
        return self.username
    


class UserHistory(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="history")
    action = models.CharField(max_length=255)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - {self.action} at {self.timestamp}"

