from django.db import models 

class Jobs(models.Model):
    url = models.CharField(max_length=1000)
    group = models.ForeignKey('Group',on_delete=models.CASCADE)

class Queries(models.Model):
    query = models.CharField(max_length=100000)
    group = models.ForeignKey('Group',on_delete=models.CASCADE)

class RequiredData(models.Model):
    word = models.CharField(max_length=100000)
    score = models.IntegerField()
    group = models.ForeignKey('Group',on_delete=models.CASCADE)

class Group(models.Model):
    group = models.CharField(max_length=100000)
    threshold_value = models.IntegerField()
    num_results = models.IntegerField()