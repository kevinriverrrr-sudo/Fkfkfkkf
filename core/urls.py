from django.urls import path
from . import views


urlpatterns = [
    path('', views.index, name='index'),
    path('upload/', views.upload_video, name='upload'),
    path('video/<int:pk>/', views.video_detail, name='video_detail'),
    path('video/<int:pk>/comment/', views.add_comment, name='add_comment'),
    path('video/<int:pk>/react/', views.react, name='react'),

    path('register/', views.register_view, name='register'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
]
