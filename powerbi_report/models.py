# powerbi_report/models.py

from django.db import models
from django.contrib.auth.models import User
from users.models import CustomUser

class PowerBIReport(models.Model):
    pass

class Report(models.Model):
    pass


class ReportAccess(models.Model):
    pass
    
class Dashboard(models.Model):
    pass

class Refresh(models.Model):
    pass
  