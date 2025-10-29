from django.contrib.auth.models import AbstractUser, Group, Permission
from django.db import models

class Role(models.Model):
    name = models.CharField(max_length=50, unique=True)
    permissions = models.ManyToManyField(Permission, blank=True) 
    description = models.CharField(max_length=255,default='no description')
    def __str__(self):
        return self.name

class CustomUser(AbstractUser):
   
    ad2000 = models.CharField(max_length=255,blank=True, null=True, unique=True,help_text="AD2000 identifier from LDAP")
    role = models.ForeignKey(Role, on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(max_length=20, default='Not Active')
    profile_image = models.ImageField(upload_to='profile_images/', null=True, blank=True)
    ldap_password = models.CharField(max_length=255, blank=True, null=True,help_text="LDAP password (store securely in production!)")
    user_permissions = models.ManyToManyField(Permission, blank=True) 

    def __str__(self):
        return self.username
    


class UserHistory(models.Model):
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name="history")
    action = models.CharField(max_length=255)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user.username} - {self.action} at {self.timestamp}"

