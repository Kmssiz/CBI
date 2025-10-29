from django.db import models
from django.conf import settings

class Task(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('complete', 'Complete'),
        ('in_progress', 'In Progress'),
    ]

    title = models.CharField(max_length=255)
    description = models.TextField()
    end_time = models.TimeField(null=True, blank=True)  
    day_of_end = models.DateField(null=True, blank=True)    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')  
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True)
   
    def __str__(self):
        return self.title
