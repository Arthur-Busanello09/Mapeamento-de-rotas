from django.urls import path
from .views import rota_carro, rota_caminhao

urlpatterns = [
    path('rota-carro', rota_carro),
    path('rota-caminhao', rota_caminhao),
]
