from django.urls import path
from . import views

urlpatterns = [path("", views.dashboard, name="dashboard"), path("preview/", views.preview, name="preview"), path("export/<str:platform>/", views.export_csv, name="export")]
