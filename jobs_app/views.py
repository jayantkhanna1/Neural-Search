from django.shortcuts import render
from django.http import HttpResponse
from .models import *
from django.http import JsonResponse
import os
from dotenv import load_dotenv 
load_dotenv()
import json
from django.core.mail import send_mail
from .tasks import *


def index(request):
    group_list = Group.objects.all()
    return render(request,'index.html',{'group_list':group_list})

def searchJobs(request):
    group = request.POST.get('group')
    search_jobs.delay(group)
    return JsonResponse({'status':'success'})

def new_group_page(request):
    return render(request,'new_group_page.html')

def createGroup(request):
    group_name = request.POST.get('group_name')
    num_results = request.POST.get('max_query')
    threshold_value = request.POST.get('threshhold_value')
    queries = json.loads(request.POST.get('queries'))
    required_data = json.loads(request.POST.get('imp_words'))
    group = Group.objects.create(group=group_name,num_results=num_results,threshold_value=threshold_value)
    group.save()
    for x in queries:
        Queries.objects.create(query=x,group=group)
    for x in required_data:
        RequiredData.objects.create(word=x['word'],score=x['score'],group=group)
    search_jobs.delay(group.id)
    return JsonResponse({'status':'success'})

def group_page(request):
    group_id = request.GET.get('group_id')
    group = Group.objects.get(id=group_id)
    queries = Queries.objects.filter(group=group_id)
    required_data = RequiredData.objects.filter(group=group_id)
    jobs = Jobs.objects.filter(group=group_id)
    # reverse jobs
    jobs = jobs[::-1]
    return render(request,'group_page.html',{'group':group,'queries':queries,'required_data':required_data,'jobs':jobs})