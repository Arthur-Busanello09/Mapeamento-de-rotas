from django.urls import path
from .views import rota_carro, rota_caminhao, geocode_search, health

urlpatterns = [
    path('rota-carro', rota_carro),
    path('rota-caminhao', rota_caminhao),
     path('geocode', geocode_search), 
     path('health', health)
]
