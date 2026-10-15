from django.urls import path

from . import views

urlpatterns = [
    path('health/', views.health, name='health'),
    path('route/', views.route, name='route'),
    path('route/map/', views.route_map, name='route-map'),
]
