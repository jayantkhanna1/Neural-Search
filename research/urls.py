from django.urls import path

from . import views

app_name = "research"

urlpatterns = [
    path("", views.index, name="index"),
    path("api/sessions/", views.list_sessions, name="list_sessions"),
    path("api/research/", views.create_session, name="create_session"),
    path("api/sessions/<int:session_id>/", views.session_detail, name="session_detail"),
    path("api/sessions/<int:session_id>/events/", views.session_events, name="session_events"),
    path("api/sessions/<int:session_id>/research/", views.add_research, name="add_research"),
    path("api/sessions/<int:session_id>/chat/", views.send_message, name="send_message"),
]
