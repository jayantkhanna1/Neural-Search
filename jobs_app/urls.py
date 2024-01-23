from django.urls import path
from . import views

urlpatterns = [
    path('', views.index, name='index'),
    path('searchJobs',views.searchJobs,name='searchJobs'),
    path('new_group_page',views.new_group_page,name='new_group_page'),
    path('create_group',views.createGroup,name='createGroup'),
    path('group_page',views.group_page,name='group_page'),
]
